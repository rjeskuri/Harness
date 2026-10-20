"""Tests for harness_lib/registry.py (ticket #91, refactor 9 of docs/proposals/refactor.md): one reader
and writer for ~/.claude/harness/projects.json, adopted by harness-projects (strict), harness-board,
harness-standup and harness-digest; harness-pr keeps its own reader.

- The tools, as they behave today and must keep behaving (broad, through the command line, each in a
  temporary HOME): the four messages for an unreadable registry (not valid JSON, cannot read, wrong
  shape, wrong version) word for word from all four tools; harness-projects refuses an entry without
  a path (`project N has no path`, N counted from 1) where board, standup and digest skip it; the
  standup's `--json` projects keep exactly today's keys and values (name, path, billable, client and
  the report fields; no notes, no unknown keys); `harness-standup <key>` picks by exact name, any
  case, or path; harness-projects keeps unknown entry keys through a write; harness-pr's reader still
  turns a malformed registry into an empty table and says `cannot read` on bad JSON.
- The tools read through the lib: the plugin copied into a temporary tree whose
  lib/harness_lib/registry.py wraps the real one (messages prefixed SENTINEL, the project list
  reversed, saved entries marked). Each tool's messages, order and writes follow the wrapper.
- The lib's API directly: Project (path, name, billable=False, client="", notes="", extra={}) with
  from_entry/to_entry, registry_path, load_registry(strict=False), save_registry, find_project; every
  expected value is computed here from the registry schema.
- The messages exist once: across the four tools and lib/harness_lib/, each registry message is one
  string literal, in registry.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_harness_lib_registry.py" -v
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True  # loading the lib must not leave __pycache__ in the checkout

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
BIN = PLUGIN / "bin"
LIB = PLUGIN / "lib"
REGISTRY_PY = LIB / "harness_lib" / "registry.py"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
GIT = shutil.which("git")
READERS = ("harness-projects", "harness-board", "harness-standup", "harness-digest")
# The arguments that make each tool read the registry before anything else (no gh, no repository needed).
READ_ARGS = {
    "harness-projects": ["list"],
    "harness-board": ["project", "sync", "--all-projects"],
    "harness-standup": ["--json"],
    "harness-digest": ["--json"],
}
# The keys of one project in `harness-standup --json`, today.
STANDUP_KEYS = {
    "name", "path", "billable", "client", "error", "git", "last_session", "next", "latest_spec",
    "hours_this_month", "sessions", "board", "prs_awaiting", "needs_input", "stale_worktrees", "board_error",
}
KNOWN = ("path", "name", "billable", "client", "notes")
# Entries that are not a project with a path: skipped by board, standup and digest, refused by harness-projects.
PATHLESS = ({"name": "nopath"}, {"path": ""}, {"path": "   "}, {"path": 5, "name": "five"}, {"path": None},
            "just a string", ["a", "list"], 7, None)


# --------------------------------------------------------------- expectations, from the schema
def reg_file(home) -> str:
    return os.path.join(str(home), ".claude", "harness", "projects.json")


def default_name(path: str) -> str:
    return os.path.basename(path.rstrip("/")) or path


def entry(p: dict) -> dict:
    """A registry entry with every schema field filled in, unknown keys kept (harness-projects today)."""
    out = dict(p)
    out["name"] = str(p.get("name") or default_name(p["path"]))
    out["billable"] = bool(p.get("billable", False))
    out["client"] = str(p.get("client") or "")
    out["notes"] = str(p.get("notes") or "")
    return out


def has_path(p) -> bool:
    return isinstance(p, dict) and isinstance(p.get("path"), str) and bool(p["path"].strip())


def json_error(text: str) -> str:
    try:
        json.loads(text)
    except ValueError as err:
        return str(err)
    raise AssertionError(f"{text!r} is valid JSON")


def msg_invalid(path, text):
    return f"{path} is not valid JSON ({json_error(text)}); fix it or remove it"


def msg_shape(path):
    return f'{path}: expected {{"version": 1, "projects": [...]}}'


def msg_version(path, version):
    return f"{path} is version {version!r}; this tool reads version 1"


def msg_unreadable(path, strerror):
    return f"cannot read {path}: {strerror}"


def msg_no_path(path, n):
    return f"{path}: project {n} has no path"


INVALID_JSON = ("{not json", "", "[1,", '{"version": 1, "projects": [}', "{'version': 1}")
WRONG_SHAPE = ([], {"version": 1}, {"projects": {}}, {"projects": "a"}, {"version": 1, "projects": None}, None, 42,
               "projects")
WRONG_VERSION = (2, "1", None, 0, [1])


def bad_registries(path):
    """(file text, the message every reader gives) for each kind of unreadable registry but `cannot read`."""
    cases = [(text, msg_invalid(path, text)) for text in INVALID_JSON]
    cases += [(json.dumps(data), msg_shape(path)) for data in WRONG_SHAPE]
    cases += [(json.dumps({"version": v, "projects": []}), msg_version(path, v)) for v in WRONG_VERSION]
    return cases


# --------------------------------------------------------------- running things
def tool_env(home, **extra) -> dict:
    folders = ([os.path.dirname(GIT)] if GIT and os.path.dirname(GIT) not in ("/usr/bin", "/bin") else []) + [
        SYSTEM_PATH]
    env = {
        "PATH": os.pathsep.join(folders),
        "HOME": str(home),
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CEILING_DIRECTORIES": str(home),
        "HARNESS_PROJECT": "o/7",          # harness-board project sync needs a board before it reads the registry
        "HARNESS_NOTIFY_SOUND": "",
    }
    env.update(extra)
    return env


def run(bin_dir, tool, args, home, cwd=None):
    return subprocess.run([sys.executable, os.path.join(str(bin_dir), tool)] + list(args), cwd=cwd or str(home),
                          env=tool_env(home), capture_output=True, text=True, timeout=60)


def write_registry(home, data=None, text=None) -> str:
    path = reg_file(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text if text is not None else json.dumps(data))
    return path


def board_skipped(stderr: str) -> list:
    return re.findall(r"the registered project (.+?) is skipped: ", stderr)


def read_names(tool, proc) -> list:
    """The (name or None, path) pairs a reader reported, in the order it reported them."""
    if tool == "harness-board":
        return [(None, p) for p in board_skipped(proc.stderr)]
    data = json.loads(proc.stdout)
    if tool == "harness-standup":
        return [(p["name"], p["path"]) for p in data["projects"]]
    if tool == "harness-digest":
        return [(p["name"], p["path"]) for p in data["projects"] + data["skipped"]]
    return [(p["name"], p["path"]) for p in data["projects"]]


class TempHome(unittest.TestCase):
    def setUp(self) -> None:
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="harness-registry-"))
        self.addCleanup(shutil.rmtree, self.home, True)

    def assertOneLine(self, proc, tool, message):
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(proc.stderr.strip(), f"{tool}: {message}")


# =============================================================== the tools, as they behave today
class ToolsReadTheRegistry(TempHome):
    """What the four readers (and harness-pr) do with the registry; the refactor must keep all of it."""

    def test_unreadable_registry_messages_from_every_reader(self):
        path = reg_file(self.home)
        for text, message in bad_registries(path):
            write_registry(self.home, text=text)
            for tool in READERS:
                with self.subTest(tool=tool, registry=text):
                    self.assertOneLine(run(BIN, tool, READ_ARGS[tool], self.home), tool, message)

    def test_registry_that_cannot_be_opened(self):
        path = reg_file(self.home)
        os.makedirs(path)                                   # a folder where the file should be
        for tool in READERS:
            with self.subTest(tool=tool):
                self.assertOneLine(run(BIN, tool, READ_ARGS[tool], self.home), tool,
                                   msg_unreadable(path, "Is a directory"))

    def test_accepted_versions_and_extra_top_level_keys(self):
        gone = os.path.join(self.home, "gone")
        for data in ({"projects": [{"path": gone}]}, {"version": 1, "projects": [{"path": gone}], "note": "x"}):
            write_registry(self.home, data)
            for tool in ("harness-standup", "harness-digest", "harness-projects"):
                with self.subTest(tool=tool, data=data):
                    proc = run(BIN, tool, READ_ARGS[tool] + (["--json"] if tool == "harness-projects" else []),
                               self.home)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(read_names(tool, proc), [("gone", gone)])

    def test_missing_registry_is_empty(self):
        proc = run(BIN, "harness-standup", ["--json"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["projects"], [])
        proc = run(BIN, "harness-projects", ["--json"], self.home)
        self.assertEqual(json.loads(proc.stdout), {"version": 1, "projects": []})
        self.assertFalse(os.path.exists(reg_file(self.home)))

    def test_harness_projects_refuses_an_entry_without_a_path(self):
        path = reg_file(self.home)
        ok = [{"path": os.path.join(self.home, n), "name": n} for n in ("one", "two", "three")]
        layouts = []
        for i, bad in enumerate(PATHLESS):
            at = i % 4                                       # the pathless entry at index 0, 1, 2 or 3
            layouts.append((ok[:at] + [bad] + ok[at:], at + 1))
        layouts.append(([ok[0], PATHLESS[1], ok[1], PATHLESS[0]], 2))     # the first one is named
        for projects, n in layouts:
            write_registry(self.home, {"version": 1, "projects": projects})
            for args in (["list"], ["--json"], ["set", "one", "--client", "C"], ["remove", "two"]):
                with self.subTest(projects=projects, args=args):
                    self.assertOneLine(run(BIN, "harness-projects", args, self.home), "harness-projects",
                                       msg_no_path(path, n))
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["projects"], projects)      # nothing was written

    def test_board_standup_and_digest_skip_an_entry_without_a_path(self):
        names = ("alpha", "beta", "gamma", "delta")
        for shift in range(3):
            good = [{"path": os.path.join(self.home, f"{n}{shift}"), "name": f"{n.title()}{shift}"} for n in names]
            projects = []
            for i, p in enumerate(good):
                projects.append(p)
                projects.extend(PATHLESS[(i + shift) % len(PATHLESS):][:2 + shift % 2])
            if shift == 1:
                projects.insert(0, PATHLESS[-2])
            write_registry(self.home, {"version": 1, "projects": projects})
            want = [(p["name"], p["path"]) for p in good]
            for tool in ("harness-board", "harness-standup", "harness-digest"):
                with self.subTest(tool=tool, shift=shift):
                    proc = run(BIN, tool, READ_ARGS[tool], self.home)
                    if tool == "harness-board":
                        self.assertEqual(proc.returncode, 1, proc.stderr)
                        self.assertEqual(board_skipped(proc.stderr), [p for _, p in want])
                        self.assertIn("No registered projects to sync.", proc.stdout)
                    else:
                        self.assertEqual(proc.returncode, 0, proc.stderr)
                        self.assertEqual(read_names(tool, proc), want)

    def test_standup_json_projects_shape(self):
        t = self.home
        entries = [
            {"path": f"{t}/alpha", "name": "Alpha", "billable": True, "client": "Acme", "notes": "keep",
             "color": "red"},
            {"path": f"{t}/beta/"},
            {"path": f"{t}/gamma", "name": "", "billable": 1, "client": None, "notes": None},
            {"path": f"{t}/delta", "name": "Délta", "billable": "", "client": 42},
            {"path": f"{t}/eps", "billable": False, "client": "Zed Corp", "version": 9, "extra": {"k": 1}},
            {"path": f"{t}/zeta", "name": None, "billable": "no", "client": ""},
        ]
        write_registry(self.home, {"version": 1, "projects": entries})
        proc = run(BIN, "harness-standup", ["--json"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = json.loads(proc.stdout)["projects"]
        self.assertEqual(len(got), len(entries))
        for p, g in zip(entries, got):
            with self.subTest(entry=p):
                self.assertEqual(set(g), STANDUP_KEYS)
                want = {"name": str(p.get("name") or default_name(p["path"])), "path": p["path"],
                        "billable": bool(p.get("billable")), "client": str(p.get("client") or "")}
                self.assertEqual({k: g[k] for k in want}, want)
                self.assertIs(type(g["billable"]), bool)
                self.assertEqual(g["error"], "path not found")

    def test_standup_picks_one_project(self):
        t = self.home
        projects = [{"path": f"{t}/a1", "name": "Alpha"}, {"path": f"{t}/a2", "name": "ALPHA"},
                    {"path": f"{t}/b", "name": "Beta"}, {"path": f"{t}/g", "name": "G"},
                    {"path": f"{t}/same1", "name": "twin"}, {"path": f"{t}/same2", "name": "Twin"}]
        write_registry(self.home, {"version": 1, "projects": projects})
        for key, path in (("Alpha", f"{t}/a1"), ("ALPHA", f"{t}/a2"), ("beta", f"{t}/b"), ("BETA", f"{t}/b"),
                          (f"{t}/g/", f"{t}/g"), (f"{t}/a2", f"{t}/a2"), ("twin", f"{t}/same1")):
            with self.subTest(key=key):
                proc = run(BIN, "harness-standup", [key, "--json"], self.home)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual([p["path"] for p in json.loads(proc.stdout)["projects"]], [path])
        for key, message in (("alpha", f"{'alpha'!r} matches 2 projects; give its path instead"),
                             ("TWIN", f"{'TWIN'!r} matches 2 projects; give its path instead"),
                             ("nobody", f"no registered project named or at {'nobody'!r} (harness-projects list "
                                        "shows them)"),
                             (f"{t}/nowhere", f"no registered project named or at {t + '/nowhere'!r} "
                                              "(harness-projects list shows them)")):
            with self.subTest(key=key):
                self.assertOneLine(run(BIN, "harness-standup", [key, "--json"], self.home), "harness-standup",
                                   message)

    def test_harness_projects_keeps_unknown_keys_through_a_write(self):
        a, b, c = (os.path.join(self.home, n) for n in ("a", "b", "café"))
        for d in (a, b, c):
            os.makedirs(d)
        projects = [{"path": a, "name": "A", "color": "red", "tags": ["x", "y"], "billable": 1},
                    {"path": b, "client": None, "meta": {"k": [1, 2]}, "notes": "n"}]
        write_registry(self.home, {"version": 1, "projects": projects, "comment": "dropped on write"})

        proc = run(BIN, "harness-projects", ["set", "A", "--client", "Acme", "--json"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        want_a = dict(entry(projects[0]), client="Acme")
        self.assertEqual(json.loads(proc.stdout), want_a)
        with open(reg_file(self.home), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"version": 1, "projects": [want_a, entry(projects[1])]})

        proc = run(BIN, "harness-projects", ["add", c, "--billable", "--client", "Zürich"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        new = {"path": c, "name": "café", "billable": True, "client": "Zürich", "notes": ""}
        with open(reg_file(self.home), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"version": 1, "projects": [want_a, entry(projects[1]), new]})

        proc = run(BIN, "harness-projects", ["--json"], self.home)
        self.assertEqual(json.loads(proc.stdout), {"version": 1, "projects": [want_a, entry(projects[1]), new]})

        proc = run(BIN, "harness-projects", ["remove", "A"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(reg_file(self.home), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"version": 1, "projects": [entry(projects[1]), new]})
        self.assertEqual(sorted(os.listdir(os.path.dirname(reg_file(self.home)))), ["projects.json"])

    def test_harness_pr_keeps_its_own_reader(self):
        path = reg_file(self.home)
        for data in ({"version": 2, "projects": []}, {"projects": "x"}, [1], {"version": "1"}):
            write_registry(self.home, data)
            with self.subTest(data=data):
                proc = run(BIN, "harness-pr", ["awaiting", "--all-projects", "--json"], self.home)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(json.loads(proc.stdout), [])
        write_registry(self.home, text="{nope")
        proc = run(BIN, "harness-pr", ["awaiting", "--all-projects", "--json"], self.home)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stderr.strip(), f"harness-pr: cannot read {path}: {json_error('{nope')}")


# =============================================================== the tools read through the lib
SENTINEL_REGISTRY = '''"""A wrapper around the real registry module: messages prefixed, lists reversed, writes marked."""
from __future__ import annotations

import dataclasses

from harness_lib import _real_registry as _real

globals().update({k: v for k, v in vars(_real).items() if not k.startswith("__")})


def _mark(err):
    return type(err)("SENTINEL " + str(err))


def load_registry(*args, **kwargs):
    try:
        projects = _real.load_registry(*args, **kwargs)
    except Exception as err:
        raise _mark(err) from None
    return list(reversed(projects))


def find_project(*args, **kwargs):
    try:
        return _real.find_project(*args, **kwargs)
    except Exception as err:
        raise _mark(err) from None


def save_registry(projects, *args, **kwargs):
    marked = []
    for p in projects:
        if dataclasses.is_dataclass(p):
            p = dataclasses.replace(p, extra=dict(p.extra, saved_by="sentinel"))
        elif isinstance(p, dict):
            p = dict(p, saved_by="sentinel")
        marked.append(p)
    return _real.save_registry(marked, *args, **kwargs)
'''


class ToolsUseTheLib(TempHome):
    """The plugin copied with a registry.py that wraps the real one: the four tools follow the wrapper."""

    def setUp(self) -> None:
        super().setUp()
        self.assertTrue(REGISTRY_PY.is_file(), f"{REGISTRY_PY} does not exist")
        self.tree = os.path.join(self.home, "plugin")
        shutil.copytree(str(PLUGIN), self.tree, ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"))
        pkg = os.path.join(self.tree, "lib", "harness_lib")
        shutil.copy(str(REGISTRY_PY), os.path.join(pkg, "_real_registry.py"))
        with open(os.path.join(pkg, "registry.py"), "w", encoding="utf-8") as fh:
            fh.write(SENTINEL_REGISTRY)
        self.bin = os.path.join(self.tree, "bin")

    def test_messages_come_from_the_lib(self):
        path = reg_file(self.home)
        cases = [("{bad", msg_invalid(path, "{bad")), ("[]", msg_shape(path)),
                 (json.dumps({"version": 3, "projects": []}), msg_version(path, 3))]
        for text, message in cases:
            write_registry(self.home, text=text)
            for tool in READERS:
                with self.subTest(tool=tool, registry=text):
                    self.assertOneLine(run(self.bin, tool, READ_ARGS[tool], self.home), tool, "SENTINEL " + message)
        os.remove(path)
        os.makedirs(path)
        for tool in READERS:
            with self.subTest(tool=tool, registry="a folder"):
                self.assertOneLine(run(self.bin, tool, READ_ARGS[tool], self.home), tool,
                                   "SENTINEL " + msg_unreadable(path, "Is a directory"))

    def test_harness_projects_reads_strictly_through_the_lib(self):
        path = reg_file(self.home)
        for at in (1, 3):
            projects = [{"path": os.path.join(self.home, f"p{i}")} for i in range(3)]
            projects.insert(at - 1, {"name": "nopath"})
            write_registry(self.home, {"version": 1, "projects": projects})
            with self.subTest(at=at):
                self.assertOneLine(run(self.bin, "harness-projects", ["list"], self.home), "harness-projects",
                                   "SENTINEL " + msg_no_path(path, at))

    def test_every_reader_takes_the_lib_list(self):
        good = [{"path": os.path.join(self.home, n), "name": n.upper()} for n in ("one", "two", "three", "four")]
        write_registry(self.home, {"version": 1, "projects": [good[0], {"name": "x"}, good[1], good[2], good[3]]})
        want = [(p["name"], p["path"]) for p in reversed(good)]
        for tool in ("harness-board", "harness-standup", "harness-digest"):
            with self.subTest(tool=tool):
                proc = run(self.bin, tool, READ_ARGS[tool], self.home)
                got = read_names(tool, proc)
                self.assertEqual(got, [(None, p) for _, p in want] if tool == "harness-board" else want,
                                 proc.stderr)
        write_registry(self.home, {"version": 1, "projects": good})
        proc = run(self.bin, "harness-projects", ["--json"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(read_names("harness-projects", proc), want)

    def test_standup_picks_through_the_lib(self):
        write_registry(self.home, {"version": 1, "projects": [{"path": os.path.join(self.home, "a"), "name": "A"}]})
        self.assertOneLine(run(self.bin, "harness-standup", ["zzz"], self.home), "harness-standup",
                           f"SENTINEL no registered project named or at {'zzz'!r} (harness-projects list shows them)")

    def test_harness_projects_writes_through_the_lib(self):
        a, b = os.path.join(self.home, "a"), os.path.join(self.home, "b")
        os.makedirs(a)
        os.makedirs(b)
        write_registry(self.home, {"version": 1, "projects": [{"path": a, "name": "A", "color": "red"}]})
        proc = run(self.bin, "harness-projects", ["add", b], self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(reg_file(self.home), encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(sorted(p["path"] for p in saved["projects"]), [a, b])
        self.assertEqual([p.get("saved_by") for p in saved["projects"]], ["sentinel", "sentinel"])
        self.assertEqual([p.get("color") for p in saved["projects"] if p["path"] == a], ["red"])


# =============================================================== the lib's API
def registry_module():
    lib = str(LIB)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    return importlib.import_module("harness_lib.registry")


def cli_problem():
    return importlib.import_module("harness_lib.cli").Problem


class RegistryModule(TempHome):
    def setUp(self) -> None:
        super().setUp()
        self.reg = registry_module()
        patcher = mock.patch.dict(os.environ, {"HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = reg_file(self.home)

    def expect(self, p: dict):
        """The Project an entry becomes, built from the schema."""
        e = entry(p)
        return self.reg.Project(path=e["path"], name=e["name"], billable=e["billable"], client=e["client"],
                                notes=e["notes"], extra={k: v for k, v in p.items() if k not in KNOWN})

    def test_exports(self):
        for name in ("Project", "registry_path", "load_registry", "save_registry", "find_project"):
            self.assertTrue(hasattr(self.reg, name), name)
        self.assertTrue(dataclasses.is_dataclass(self.reg.Project))
        self.assertEqual({f.name for f in dataclasses.fields(self.reg.Project)}, set(KNOWN) | {"extra"})

    def test_project_defaults(self):
        p = self.reg.Project("/x/proj", "Proj")
        self.assertEqual((p.path, p.name, p.billable, p.client, p.notes, p.extra), ("/x/proj", "Proj", False, "", "",
                                                                                     {}))
        q = self.reg.Project("/y", "Y")
        p.extra["k"] = 1
        self.assertEqual(q.extra, {})                        # no shared default

    def test_registry_path_follows_home(self):
        self.assertEqual(self.reg.registry_path(), self.path)
        other = os.path.join(self.home, "elsewhere")
        with mock.patch.dict(os.environ, {"HOME": other}):
            self.assertEqual(self.reg.registry_path(), reg_file(other))

    def test_from_entry_and_to_entry(self):
        cases = [
            {"path": "/w/alpha", "name": "Alpha", "billable": True, "client": "Acme", "notes": "n"},
            {"path": "/w/beta/"},
            {"path": "/w/gamma", "name": "", "billable": 1, "client": None, "notes": None},
            {"path": "/w/delta", "name": None, "billable": "", "client": 42, "notes": 7},
            {"path": "~/eps", "color": "red", "tags": ["a"], "meta": {"k": {"j": 1}}},
            {"path": "/", "billable": 0},
            {"path": "rel/zeta", "name": "Zéta", "billable": [], "client": "C", "version": 2},
        ]
        for p in cases:
            with self.subTest(entry=p):
                got = self.reg.Project.from_entry(p)
                self.assertEqual(got, self.expect(p))
                self.assertIs(type(got.billable), bool)
                self.assertEqual(got.to_entry(), entry(p))
                self.assertEqual(self.reg.Project.from_entry(got.to_entry()), got)

    def test_load_missing_registry_is_empty(self):
        for strict in (False, True):
            self.assertEqual(self.reg.load_registry(strict=strict), [])
        self.assertEqual(self.reg.load_registry(), [])
        self.assertFalse(os.path.exists(self.path))

    def test_load_registry_in_order(self):
        entries = [{"path": f"/w/p{i}", "name": f"P{i}", "billable": i % 2, "client": "c" * i, "x": i}
                   for i in range(5)] + [{"path": "/w/last/"}]
        for data in ({"version": 1, "projects": entries}, {"projects": entries, "note": "top level"}):
            write_registry(self.home, data)
            for strict in (False, True):
                with self.subTest(strict=strict, data=list(data)):
                    got = self.reg.load_registry(strict=strict)
                    self.assertEqual(got, [self.expect(p) for p in entries])

    def test_entries_without_a_path(self):
        good = [{"path": f"/w/{n}", "name": n} for n in ("a", "b", "c")]
        for i, bad in enumerate(PATHLESS):
            at = i % 4
            projects = good[:at] + [bad] + good[at:] + [PATHLESS[(i + 1) % len(PATHLESS)]]
            write_registry(self.home, {"version": 1, "projects": projects})
            with self.subTest(bad=bad, at=at):
                self.assertEqual(self.reg.load_registry(), [self.expect(p) for p in good])
                self.assertEqual(self.reg.load_registry(strict=False), [self.expect(p) for p in good])
                with self.assertRaises(cli_problem()) as caught:
                    self.reg.load_registry(strict=True)
                self.assertEqual(str(caught.exception), msg_no_path(self.path, at + 1))

    def test_unreadable_registry_raises_the_lib_problem(self):
        Problem = cli_problem()
        for text, message in bad_registries(self.path):
            write_registry(self.home, text=text)
            for strict in (False, True):
                with self.subTest(registry=text, strict=strict):
                    with self.assertRaises(Problem) as caught:
                        self.reg.load_registry(strict=strict)
                    self.assertEqual(str(caught.exception), message)
        os.remove(self.path)
        os.makedirs(self.path)
        with self.assertRaises(Problem) as caught:
            self.reg.load_registry()
        self.assertEqual(str(caught.exception), msg_unreadable(self.path, "Is a directory"))

    def test_save_then_load(self):
        P = self.reg.Project
        projects = [P("/w/a", "A"), P("/w/b", "Bé", True, "Acme", "notes here", {"color": "red", "n": [1, 2]}),
                    P("/w/c/", "c", False, "", "", {"meta": {"deep": True}})]
        self.reg.save_registry(projects)                    # the folder does not exist yet
        with open(self.path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.endswith("\n"))
        want = [dict(p.extra, path=p.path, name=p.name, billable=p.billable, client=p.client, notes=p.notes)
                for p in projects]
        self.assertEqual(json.loads(text), {"version": 1, "projects": want})
        self.assertEqual(self.reg.load_registry(strict=True), projects)

        self.reg.save_registry(projects[1:])                 # over the old file
        self.assertEqual(self.reg.load_registry(), projects[1:])
        self.reg.save_registry([])
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"version": 1, "projects": []})
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["projects.json"])   # no temporary file left

    def test_find_project(self):
        P = self.reg.Project
        real = os.path.join(self.home, "real")
        os.makedirs(real)
        link = os.path.join(self.home, "link")
        os.symlink(real, link)
        projects = [P("/w/a1", "Alpha"), P("/w/a2", "ALPHA"), P("/w/b", "Beta"), P(real, "R"),
                    P("/w/t1", "twin"), P("/w/t2", "Twin")]
        for key, want in (("Alpha", 0), ("ALPHA", 1), ("beta", 2), ("BeTa", 2), ("/w/b", 2), ("/w/b/", 2),
                          ("R", 3), ("r", 3), (link, 3), (real + "/", 3), ("twin", 4), ("Twin", 5), ("/w/a2", 1)):
            with self.subTest(key=key):
                self.assertEqual(self.reg.find_project(projects, key), projects[want])
        Problem = cli_problem()
        for key, message in (("alpha", f"{'alpha'!r} matches 2 projects; give its path instead"),
                             ("TWIN", f"{'TWIN'!r} matches 2 projects; give its path instead"),
                             ("gamma", f"no registered project named or at {'gamma'!r} (harness-projects list "
                                       "shows them)"),
                             ("/w/zz", f"no registered project named or at {'/w/zz'!r} (harness-projects list "
                                       "shows them)")):
            with self.subTest(key=key):
                with self.assertRaises(Problem) as caught:
                    self.reg.find_project(projects, key)
                self.assertEqual(str(caught.exception), message)
        with self.assertRaises(Problem):
            self.reg.find_project([], "Alpha")


# =============================================================== the messages exist once
def literal_texts(source: str):
    """Every string literal outside docstrings, an f-string as its text with {} for each placeholder."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    inner = set()
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                inner.add(id(v))
                parts.append(v.value if isinstance(v, ast.Constant) else "{}")
            out.append("".join(str(p) for p in parts))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
                and id(node) not in inner):
            out.append(node.value)
    return out


MESSAGES = {
    "not valid JSON": lambda s: "is not valid JSON" in s and "fix it or remove it" in s,
    "cannot read": lambda s: s == "cannot read {}: {}",
    "expected shape": lambda s: '"projects": [...]' in s and "expected" in s,
    "is version": lambda s: "is version" in s and "this tool reads version" in s,
    "has no path": lambda s: "has no path" in s,
}


class MessagesExistOnce(unittest.TestCase):
    def sources(self):
        files = [BIN / t for t in READERS] + sorted((LIB / "harness_lib").glob("*.py"))
        return {f: f.read_text(encoding="utf-8") for f in files}

    def test_each_registry_message_is_one_literal_in_registry_py(self):
        self.assertTrue(REGISTRY_PY.is_file(), f"{REGISTRY_PY} does not exist")
        sources = self.sources()
        for what, match in MESSAGES.items():
            with self.subTest(message=what):
                where = [f.name for f, src in sources.items() for s in literal_texts(src) if match(s)]
                self.assertEqual(where, ["registry.py"])

    def test_harness_pr_keeps_its_reader(self):
        src = (BIN / "harness-pr").read_text(encoding="utf-8")
        self.assertIn("cannot read {}: {}", literal_texts(src))
        self.assertIn("projects.json", src)


if __name__ == "__main__":
    unittest.main()
