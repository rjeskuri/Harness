"""Tests for `harness-deadcode scan` (ticket #453): a standard-library dead-code scanner.

    python3 -m unittest discover -s plugins/harness/tests -p test_deadcode.py

Each case writes a small fixture tree laid out like Harness (plugins/harness/{bin,hooks,lib/harness_lib,
skills,agents,templates,tests}, scripts/, docs/, user/, .githooks/, .github/) under a temporary folder and
runs `harness-deadcode scan --repo <tree> --json`. Each test's first scan is a real process; further scans in
the same test run in process (gitrepo_support.run_in_process). The expected candidates are written out by hand
from the fixture text and docs/research/dead-code-agent.md's rules, never computed by the tool. Every fixture
holds a positive control the scan must report, so a scanner that prints nothing cannot pass.

The JSON contract these tests pin (the ticket leaves the field names open; these are the rulings):
- stdout is one object: `candidates` (proposed for removal) and `no_caller` (subcommands and flags no skill,
  agent, doc, setting or other tool calls: the owner's typed surface, reported, never a candidate).
- a candidate: `path` (repo-relative, forward slashes), `line` (1-based line of the definition), `kind`
  (`function`, `class`, `method`, `constant`, `module` or `flag`), `name` (its last dotted or spaced part is
  the definition's own name, a flag's name holds its `--flag` spelling), `refs` (`none`, `test`, `contract`,
  `test+contract`), `tests` (the repo-relative test files that reference it) and `fingerprint` (holding the
  path, kind, name and the sha256 hex of the definition's source text, in any layout).
- a `no_caller` entry: `path`, `kind` (`subcommand` or `flag`) and `name`, as for a candidate.
- with suite line data (`--lines`), each candidate has `ran_in_suite` (true or false).
- `--lines FILE` may be repeated; each FILE is a coverage.py-shaped JSON report
  (`{"meta": {...}, "files": {<path>: {"executed_lines": [..]}}}`); `meta.kind == "real"` marks a real
  (loop smoke) run, anything else is suite data. A data path matches a source file when it is the file's
  repo-relative path or ends with `/` plus it (a loop smoke runs another checkout).
- the scan exits 0 whether or not it finds candidates.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import sysconfig
import tempfile
import textwrap
import unittest

from gitrepo_support import run_in_process

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(TESTS_DIR)
TOOL = os.path.join(PLUGIN, "bin", "harness-deadcode")
ROOT = os.path.dirname(os.path.dirname(PLUGIN))  # the checkout this module sits in

P = "plugins/harness/"
BIN = P + "bin/"
HOOKS = P + "hooks/"
LIB = P + "lib/harness_lib/"
T = P + "tests/"

BASE = {
    HOOKS + "hooks.json": '{"hooks": {}}\n',
    LIB + "__init__.py": "",
    "docs/design.md": "# Harness design\n",
}

HOOKS_JSON = """\
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/wired.py"], "timeout": 10 }
        ]
      }
    ]
  }
}
"""


def dedent(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def last(name: str) -> str:
    """The definition's own name: the last part of `Class.method`, `harness_lib.mod.NAME` or `land --yes`."""
    return re.split(r"[\s.:]+", name.strip())[-1]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hexes(fingerprint) -> set:
    return set(re.findall(r"\b[0-9a-f]{64}\b", json.dumps(fingerprint)))


def is_stdlib(name: str) -> bool:
    names = getattr(sys, "stdlib_module_names", None)
    if names is not None:
        return name in names
    if name in sys.builtin_module_names:
        return True
    spec = importlib.util.find_spec(name)
    if spec is None or spec.origin is None:
        return False
    if spec.origin in ("built-in", "frozen"):
        return True
    stdlib = os.path.realpath(sysconfig.get_paths()["stdlib"])
    origin = os.path.realpath(spec.origin)
    return origin.startswith(stdlib + os.sep) and "site-packages" not in origin


class ScanCase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.assertTrue(os.path.isfile(TOOL), f"{TOOL} does not exist")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.real_done = False

    def tree(self, files: dict, name: str = "repo") -> str:
        root = os.path.join(self.tmp, name)
        for rel, text in {**BASE, **files}.items():
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        return root

    def write(self, root: str, rel: str, text: str) -> None:
        with open(os.path.join(root, rel), "w", encoding="utf-8") as fh:
            fh.write(text)

    def run_tool(self, args):
        env = dict(os.environ, HOME=self.home)
        if not self.real_done:
            self.real_done = True
            return subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True, env=env,
                                  stdin=subprocess.DEVNULL, timeout=170)
        return run_in_process(TOOL, args=args, env=env)

    def scan(self, repo: str, *extra: str, as_json: bool = True):
        args = ["scan", "--repo", repo, *(["--json"] if as_json else []), *extra]
        p = self.run_tool(args)
        self.assertEqual(p.returncode, 0, f"exit {p.returncode}\nstdout: {p.stdout[-3000:]}\nstderr: {p.stderr[-3000:]}")
        if not as_json:
            return p.stdout
        try:
            out = json.loads(p.stdout)
        except ValueError:
            self.fail(f"not JSON: {p.stdout[-3000:]}")
        self.assertIsInstance(out, dict)
        self.assertIsInstance(out.get("candidates"), list, out)
        return out

    # ------------------------------------------------------------------ lookups
    @staticmethod
    def names_in(out, path: str) -> set:
        return {last(c["name"]) for c in out["candidates"] if c["path"] == path and c["kind"] != "module"}

    def one(self, out, path: str, name: str) -> dict:
        found = [c for c in out["candidates"] if c["path"] == path and last(c["name"]) == name]
        self.assertEqual(len(found), 1, f"{path} {name}: {found}\nall: {out['candidates']}")
        return found[0]

    @staticmethod
    def reported(out, path: str, kind: str, name: str) -> bool:
        return any(e["path"] == path and e["kind"] == kind and last(e["name"]) == name
                   for e in out.get("no_caller", []))


# ====================================================================== references and refs
CORE = dedent('''
    """Core helpers. Mentions in_docstring_only, which does not count as a use."""
    from __future__ import annotations


    def by_name():
        return 1


    def by_attribute():
        return 2


    def by_import():
        return 3


    def by_string():
        return 4


    def in_docstring_only():
        return 5


    def in_comment_only():
        return 6


    def dead_plain():
        return 7


    def dead_tested():
        return 8


    def dead_contract():
        return 9


    DEAD_BOTH = ("x", "y")

    LIVE_CONST = 3


    class DeadClass:
        pass


    class LiveClass:
        def live_method(self):
            return 1

        def dead_method(self):
            return 2

        def __repr__(self):
            return "LiveClass()"
''')

ALPHA = dedent('''
    #!/usr/bin/env python3
    """harness-alpha: its docstring names in_docstring_only too."""
    from __future__ import annotations

    import sys

    from harness_lib import core
    from harness_lib.core import by_name, by_import  # noqa: F401

    TABLE = {"go": "by_string"}


    def main():
        # in_comment_only is named only in this comment
        print(by_name(), core.by_attribute(), getattr(core, TABLE["go"])(), core.LIVE_CONST)
        print(core.LiveClass().live_method())
        return 0


    if __name__ == "__main__":
        sys.exit(main())
''')

CORE_FILES = {
    LIB + "core.py": CORE,
    BIN + "harness-alpha": ALPHA,
    P + "skills/alpha/SKILL.md": "---\nname: alpha\n---\nRun `harness-alpha`.\n",
    T + "test_core.py": dedent('''
        import unittest

        from harness_lib import core


        class CoreTest(unittest.TestCase):
            def test_dead_ones(self):
                self.assertEqual(core.dead_tested(), 8)
                self.assertEqual(core.DEAD_BOTH, ("x", "y"))
    '''),
    T + "test_more.py": dedent('''
        import unittest

        from harness_lib.core import DEAD_BOTH


        class MoreTest(unittest.TestCase):
            def test_both(self):
                self.assertIn("x", DEAD_BOTH)
    '''),
    T + "core_support.py": "def unused_support_helper():\n    return 0\n",
    "docs/design.md": "# Harness design\n\n### harness_lib modules\n\n- `core.py`: `dead_contract()` and `DEAD_BOTH`.\n",
    "docs/research/notes.md": "# Notes\n\ndead_plain and dead_method were looked at.\n",
}


class References(ScanCase):
    def test_candidates_and_their_refs(self):
        """Names, attributes, imports and non-docstring string constants are uses; docstrings, comments,
        docs/research and tests are not; docs/design.md marks `contract`, tests mark `test`."""
        out = self.scan(self.tree(CORE_FILES))
        core = LIB + "core.py"
        expected = {  # name: (kind, refs, tests)
            "in_docstring_only": ("function", "none", []),
            "in_comment_only": ("function", "none", []),
            "dead_plain": ("function", "none", []),
            "dead_tested": ("function", "test", [T + "test_core.py"]),
            "dead_contract": ("function", "contract", []),
            "DEAD_BOTH": ("constant", "test+contract", [T + "test_core.py", T + "test_more.py"]),
            "DeadClass": ("class", "none", []),
            "dead_method": ("method", "none", []),
        }
        self.assertEqual({(c["path"], last(c["name"])) for c in out["candidates"]},
                         {(core, n) for n in expected})
        for name, (kind, refs, tests) in expected.items():
            c = self.one(out, core, name)
            self.assertEqual((c["kind"], c["refs"], sorted(c["tests"])), (kind, refs, sorted(tests)), name)

    def test_text_files_that_name_code_are_uses(self):
        """Skills, agents, docs/guide.md, user/, .githooks/, .github/, scripts/*.sh and templates/ name code;
        docs/design.md marks it `contract` without keeping it; docs/research does nothing."""
        named = "".join(f"def {n}():\n    return 0\n\n\n" for n in (
            "named_in_skill", "named_in_agent", "named_in_guide", "named_in_user", "named_in_githooks",
            "named_in_github", "named_in_sh", "named_in_template", "named_in_design", "named_in_research",
            "named_nowhere"))
        files = {
            LIB + "named.py": named,
            BIN + "harness-beta": "#!/usr/bin/env python3\nimport harness_lib.named\n",
            P + "skills/s/SKILL.md": "Run `python3 -c 'from harness_lib.named import named_in_skill'`.\n",
            P + "agents/a.md": "---\nname: a\n---\nThe agent calls named_in_agent.\n",
            "docs/guide.md": "# Guide\n\n`named_in_guide` does the work.\n",
            "user/settings.snippet.json": '{"permissions": {"allow": ["Bash(named_in_user:*)"]}}\n',
            ".githooks/pre-commit": "#!/bin/sh\npython3 -c 'import harness_lib.named as n; n.named_in_githooks()'\n",
            ".github/workflows/ci.yml": "jobs:\n  t:\n    steps:\n      - run: echo named_in_github\n",
            "scripts/verify.sh": "#!/bin/sh\necho named_in_sh\n",
            P + "templates/hook.sh.tmpl": "#!/bin/sh\n# runs named_in_template\n",
            "docs/design.md": "# Harness design\n\n- `named.py`: `named_in_design()`.\n",
            "docs/research/x.md": "# Research\n\nnamed_in_research is unused.\n",
        }
        out = self.scan(self.tree(files))
        named_py = LIB + "named.py"
        self.assertEqual({(c["path"], last(c["name"])) for c in out["candidates"]},
                         {(named_py, n) for n in ("named_in_design", "named_in_research", "named_nowhere")})
        self.assertEqual(self.one(out, named_py, "named_in_design")["refs"], "contract")
        self.assertEqual(self.one(out, named_py, "named_in_research")["refs"], "none")
        self.assertEqual(self.one(out, named_py, "named_nowhere")["refs"], "none")

    def test_harness_lib_modules_are_used_by_import(self):
        files = {
            LIB + "imported_plain.py": '"""Imported as a module."""\n',
            LIB + "imported_from.py": '"""Imported from the package."""\n',
            LIB + "imported_member.py": "def thing():\n    return 1\n",
            LIB + "orphan_mod.py": '"""Nothing imports it."""\n',
            LIB + "tested_mod.py": '"""Only a test imports it."""\n',
            BIN + "harness-gamma": dedent('''
                #!/usr/bin/env python3
                import harness_lib.imported_plain
                from harness_lib import imported_from
                from harness_lib.imported_member import thing

                print(harness_lib.imported_plain, imported_from, thing())
            '''),
            T + "test_mods.py": "import unittest\n\nfrom harness_lib import tested_mod\n",
        }
        out = self.scan(self.tree(files))
        mods = {c["path"]: c for c in out["candidates"] if c["kind"] == "module"}
        self.assertEqual(set(mods), {LIB + "orphan_mod.py", LIB + "tested_mod.py"}, out["candidates"])
        self.assertEqual(mods[LIB + "orphan_mod.py"]["refs"], "none")
        self.assertEqual(mods[LIB + "tested_mod.py"]["refs"], "test")
        self.assertEqual(mods[LIB + "tested_mod.py"]["tests"], [T + "test_mods.py"])
        self.assertEqual([c for c in out["candidates"] if c["kind"] != "module"], [])

    def test_text_output_names_each_candidate(self):
        files = {
            LIB + "t.py": "def dead_text_fn():\n    return 1\n\n\ndef live_text_fn():\n    return 2\n",
            BIN + "harness-alpha": "#!/usr/bin/env python3\nfrom harness_lib.t import live_text_fn\n\nlive_text_fn()\n",
        }
        text = self.scan(self.tree(files), as_json=False)
        self.assertIn("dead_text_fn", text)
        self.assertIn(LIB + "t.py", text)
        self.assertNotIn("live_text_fn", text)


# ====================================================================== exclusion rules
class EntryPoints(ScanCase):
    def test_bin_files_hooks_json_main_and_templates(self):
        """Every bin file and every hook hooks.json names is an entry point, `main` is one, templates/ are
        other projects' code; their unused helpers are still candidates, and a hook nothing names is one."""
        files = {
            HOOKS + "hooks.json": HOOKS_JSON,
            BIN + "harness-gamma": dedent('''
                #!/usr/bin/env python3
                import sys


                def helper():
                    return 0


                def unused_in_bin():
                    return 1


                def main():
                    return helper()


                if __name__ == "__main__":
                    sys.exit(main())
            '''),
            BIN + "harness-delta": "#!/usr/bin/env python3\nimport sys\n\n\ndef main():\n    return 0\n\n\nsys.exit(main())\n",
            P + "agents/coder.md": "---\nname: coder\n---\nRun `harness-gamma`.\n",
            HOOKS + "wired.py": dedent('''
                #!/usr/bin/env python3
                import sys


                def decide(event):
                    return event


                def unused_in_hook():
                    return 2


                def main():
                    decide(sys.stdin.read())
                    return 0


                if __name__ == "__main__":
                    sys.exit(main())
            '''),
            HOOKS + "stray.py": dedent('''
                #!/usr/bin/env python3
                import sys


                def stray_helper():
                    return 0


                def main():
                    return stray_helper()


                if __name__ == "__main__":
                    sys.exit(main())
            '''),
            "scripts/tool.py": dedent('''
                import sys


                def setup():
                    return 0


                def scripts_unused():
                    return 3


                def main():
                    return 0


                if __name__ == "__main__":
                    setup()
                    sys.exit(main())
            '''),
            "scripts/other.py": "def main():\n    return 0\n",
            P + "templates/deploy_guard.py": "def template_only():\n    return 0\n",
            P + "templates/test_smoke.py": "import {{pkg}}\n\n\ndef test_import():\n    assert {{pkg}}\n",
        }
        out = self.scan(self.tree(files))
        cands = out["candidates"]
        self.assertTrue(any(c["path"] == HOOKS + "stray.py" for c in cands), cands)
        rest = {(c["path"], last(c["name"])) for c in cands if c["path"] != HOOKS + "stray.py"}
        self.assertEqual(rest, {(BIN + "harness-gamma", "unused_in_bin"), (HOOKS + "wired.py", "unused_in_hook"),
                                ("scripts/tool.py", "scripts_unused")})


class KeepComment(ScanCase):
    def test_deadcode_keep_comment(self):
        keepers = dedent('''
            def kept_same_line():  # deadcode: keep (called from a LaunchAgent plist)
                return 1


            # deadcode: keep (the owner runs it by hand)
            def kept_line_above():
                return 2


            def not_kept():  # keep this, it is handy
                return 3


            KEPT_CONST = 4  # deadcode: keep (read by an old session)
        ''')
        stub = dedent('''
            #!/usr/bin/env python3
            # deadcode: keep (a stub for sessions that started before it was unwired)
            """A retired hook."""
            import sys


            def main():
                return 0


            if __name__ == "__main__":
                sys.exit(main())
        ''')
        files = {
            LIB + "keepers.py": keepers,
            BIN + "harness-alpha": "#!/usr/bin/env python3\nimport harness_lib.keepers\n",
            HOOKS + "legacy.py": stub,
            HOOKS + "unkept.py": stub.replace("# deadcode: keep (a stub for sessions that started before it was unwired)\n", ""),
        }
        out = self.scan(self.tree(files))
        self.assertEqual(self.names_in(out, LIB + "keepers.py"), {"not_kept"})
        self.assertEqual([c for c in out["candidates"] if c["path"] == HOOKS + "legacy.py"], [])
        self.assertTrue(any(c["path"] == HOOKS + "unkept.py" for c in out["candidates"]), out["candidates"])


class RecordsAndProtocols(ScanCase):
    def test_dataclass_namedtuple_fields_and_library_protocols(self):
        records = dedent('''
            from __future__ import annotations

            import argparse
            import signal
            import sys
            from dataclasses import dataclass, field
            from typing import NamedTuple


            @dataclass
            class Entry:
                path: str
                locked: bool = False
                prunable: bool = False
                tags: list = field(default_factory=list)

                def __post_init__(self):
                    self.path = str(self.path)

                def record_unused(self):
                    return self.path


            class Row(NamedTuple):
                label: str
                size: int


            class Parser(argparse.ArgumentParser):
                def error(self, message):
                    sys.exit(f"{self.prog}: error: {message}")

                def parser_unused(self):
                    return self.prog


            def on_term(signum, frame):
                sys.exit(128 + signum)


            def install():
                signal.signal(signal.SIGTERM, on_term)
                signal.signal(signal.SIGINT, lambda signum, frame: sys.exit(130))
                ap = Parser(prog="x")
                sub = ap.add_subparsers(dest="cmd")
                sub.required = True
                return Entry("p"), Row("a", 1), ap
        ''')
        files = {
            LIB + "records.py": records,
            BIN + "harness-alpha": "#!/usr/bin/env python3\nfrom harness_lib import records\n\nrecords.install()\n",
        }
        out = self.scan(self.tree(files))
        self.assertEqual({(c["path"], last(c["name"])) for c in out["candidates"]},
                         {(LIB + "records.py", "record_unused"), (LIB + "records.py", "parser_unused")})


class SubcommandsAndFlags(ScanCase):
    def test_flags_subcommands_and_the_board_call_form(self):
        board = dedent('''
            #!/usr/bin/env python3
            from __future__ import annotations

            import argparse
            import sys


            def main():
                ap = argparse.ArgumentParser(prog="harness-board")
                sub = ap.add_subparsers(dest="cmd")
                sub.required = True
                close = sub.add_parser("close", help="close a ticket")
                close.add_argument("number", type=int)
                check = sub.add_parser("epic-check", help="close a finished epic")
                check.add_argument("number", type=int)
                orphan = sub.add_parser("orphan", help="nobody calls this")
                orphan.add_argument("--lonely", action="store_true")
                args = ap.parse_args()
                if args.cmd == "orphan":
                    print("on" if args.lonely else "off")
                    return 0
                print(args.cmd, args.number)
                return 0


            if __name__ == "__main__":
                sys.exit(main())
        ''')
        pr = dedent('''
            #!/usr/bin/env python3
            from __future__ import annotations

            import argparse
            import subprocess
            import sys


            def board(repo, *args):
                return subprocess.run(["harness-board", *args], cwd=repo, capture_output=True, text=True).stdout


            def work(*args):
                return subprocess.run(["harness-work", *args], capture_output=True, text=True).stdout


            def main():
                ap = argparse.ArgumentParser(prog="harness-pr")
                sub = ap.add_subparsers(dest="cmd")
                sub.required = True
                ld = sub.add_parser("land", help="land a PR")
                ld.add_argument("number", type=int)
                ld.add_argument("--yes", action="store_true", help="accepted and ignored")
                ld.add_argument("--dry-run", action="store_true")
                ld.add_argument("--max-rounds", dest="rounds", type=int, default=3)
                args = ap.parse_args()
                if args.dry_run:
                    return 0
                board(".", "close", str(args.number))
                board(".", "epic-check", "1")
                work("dispatch", "--after", str(args.number))
                print(args.rounds)
                return 0


            if __name__ == "__main__":
                sys.exit(main())
        ''')
        work = dedent('''
            #!/usr/bin/env python3
            import argparse


            def main():
                ap = argparse.ArgumentParser(prog="harness-work")
                sub = ap.add_subparsers(dest="cmd")
                sub.required = True
                d = sub.add_parser("dispatch")
                d.add_argument("--after", type=int)
                args = ap.parse_args()
                print(args.after)


            main()
        ''')
        runner = dedent('''
            #!/usr/bin/env python3
            import argparse


            def main():
                ap = argparse.ArgumentParser(prog="harness-runner")
                sub = ap.add_subparsers(dest="cmd")
                sub.required = True
                sub.add_parser("upgrade", help="rebuild the image")
                args = ap.parse_args()
                print(args.cmd)


            main()
        ''')
        files = {
            BIN + "harness-board": board,
            BIN + "harness-pr": pr,
            BIN + "harness-work": work,
            BIN + "harness-runner": runner,
            P + "skills/land/SKILL.md": "---\nname: land\n---\nRun `harness-pr land P --dry-run`, then harness-runner and harness-board.\n",
            T + "test_pr.py": "import unittest\n\nARGS = ['land', '34', '--yes']\n",
            "docs/design.md": dedent('''
                # Harness design

                ## bin tools

                ### harness-pr

                - `land P [--yes] [--dry-run]`: lands the PR.

                ### harness-runner

                - `upgrade`: rebuilds the image.
            '''),
        }
        out = self.scan(self.tree(files))
        # The one candidate: a flag parsed and never read. --dry-run is read; --max-rounds is read through its dest.
        self.assertEqual(len(out["candidates"]), 1, out["candidates"])
        yes = out["candidates"][0]
        self.assertEqual((yes["path"], yes["kind"], last(yes["name"])), (BIN + "harness-pr", "flag", "--yes"))
        self.assertEqual(yes["refs"], "test+contract")
        self.assertEqual(yes["tests"], [T + "test_pr.py"])
        # The owner's typed surface: reported as having no caller, never a candidate.
        for path, kind, name in ((BIN + "harness-board", "subcommand", "orphan"),
                                 (BIN + "harness-board", "flag", "--lonely"),
                                 (BIN + "harness-pr", "flag", "--max-rounds"),
                                 (BIN + "harness-runner", "subcommand", "upgrade")):
            self.assertTrue(self.reported(out, path, kind, name), f"{path} {name} not reported: {out.get('no_caller')}")
        # Called by another tool, as a separate string constant, or named by a skill.
        for path, kind, name in ((BIN + "harness-board", "subcommand", "close"),
                                 (BIN + "harness-board", "subcommand", "epic-check"),
                                 (BIN + "harness-work", "subcommand", "dispatch"),
                                 (BIN + "harness-work", "flag", "--after"),
                                 (BIN + "harness-pr", "subcommand", "land"),
                                 (BIN + "harness-pr", "flag", "--dry-run")):
            self.assertFalse(self.reported(out, path, kind, name), f"{path} {name} has a caller: {out.get('no_caller')}")


# ====================================================================== JSON and fingerprint
DEAD_FN_SRC = "def dead_fn(x):\n    y = x + 1\n    return y"
DEAD_SET_SRC = 'DEAD_SET = {"a", "b"}'
FP_HEAD = '"""Fingerprint fixture."""\nfrom __future__ import annotations\n\n\n'
FP_BODY = DEAD_FN_SRC + "\n\n\n" + DEAD_SET_SRC + "\n\n\ndef other_dead():\n    return 0\n"


class JsonAndFingerprint(ScanCase):
    def test_json_fields_and_a_fingerprint_that_follows_the_source(self):
        root = self.tree({LIB + "fp.py": FP_HEAD + FP_BODY,
                          BIN + "harness-alpha": "#!/usr/bin/env python3\nimport harness_lib.fp\n"})
        path = LIB + "fp.py"
        out = self.scan(root)
        fn, const = self.one(out, path, "dead_fn"), self.one(out, path, "DEAD_SET")
        for c in (fn, const):
            self.assertLessEqual({"path", "line", "kind", "name", "refs", "tests", "fingerprint"}, set(c), c)
            self.assertEqual(c["tests"], [])
            self.assertEqual(c["refs"], "none")
            fp = json.dumps(c["fingerprint"])
            for part in (path, c["kind"]):
                self.assertIn(part, fp)
        self.assertIn("dead_fn", json.dumps(fn["fingerprint"]))
        self.assertIn("DEAD_SET", json.dumps(const["fingerprint"]))
        text = FP_HEAD + FP_BODY
        self.assertEqual(fn["line"], text[:text.index("def dead_fn")].count("\n") + 1)
        self.assertEqual(const["line"], text[:text.index("DEAD_SET =")].count("\n") + 1)
        self.assertTrue(hexes(fn["fingerprint"]) & {sha(DEAD_FN_SRC), sha(DEAD_FN_SRC + "\n")}, fn["fingerprint"])
        self.assertTrue(hexes(const["fingerprint"]) & {sha(DEAD_SET_SRC), sha(DEAD_SET_SRC + "\n")}, const["fingerprint"])
        other = self.one(out, path, "other_dead")["fingerprint"]

        # Lines added above it and another definition changed: dead_fn's fingerprint stays, other_dead's moves.
        shifted = FP_HEAD + "def live_one():\n    return 1\n\n\nlive_one()\n\n\n" + FP_BODY.replace("return 0", "return 99")
        self.write(root, path, shifted)
        out2 = self.scan(root)
        self.assertEqual(self.one(out2, path, "dead_fn")["fingerprint"], fn["fingerprint"])
        self.assertEqual(self.one(out2, path, "DEAD_SET")["fingerprint"], const["fingerprint"])
        self.assertNotEqual(self.one(out2, path, "other_dead")["fingerprint"], other)
        self.assertGreater(self.one(out2, path, "dead_fn")["line"], fn["line"])

        # dead_fn's own source changed: a new fingerprint, whose sha is the new source's.
        changed_src = DEAD_FN_SRC.replace("x + 1", "x + 2")
        self.write(root, path, FP_HEAD + FP_BODY.replace(DEAD_FN_SRC, changed_src))
        out3 = self.scan(root)
        new = self.one(out3, path, "dead_fn")["fingerprint"]
        self.assertNotEqual(new, fn["fingerprint"])
        self.assertTrue(hexes(new) & {sha(changed_src), sha(changed_src + "\n")}, new)


# ====================================================================== --lines
M_PY = dedent('''
    from __future__ import annotations


    def ran_live(x):
        return x * 2


    def ran_suite_only(x):
        return x * 3


    def never_ran(x):
        return x * 4


    CONST_A = 5
''')


def line_of(text: str, needle: str) -> int:
    return text[:text.index(needle)].count("\n") + 1


class Lines(ScanCase):
    def test_real_runs_drop_and_the_suite_marks(self):
        root = self.tree({LIB + "m.py": M_PY, BIN + "harness-alpha": "#!/usr/bin/env python3\nimport harness_lib.m\n"})
        rel = LIB + "m.py"
        imported = [line_of(M_PY, s) for s in ("from __future__", "def ran_live", "def ran_suite_only",
                                                "def never_ran", "CONST_A =")]  # what an import alone runs
        live_body, suite_body = line_of(M_PY, "return x * 2"), line_of(M_PY, "return x * 3")
        real = {"meta": {"kind": "real"},
                "files": {"/Users/someone/elsewhere/checkout/" + rel: {"executed_lines": sorted(imported + [live_body])}}}
        suite = {"meta": {"version": "7.10.7"},  # a coverage.py report: suite data
                 "files": {os.path.join(root, rel): {"executed_lines": sorted(imported + [live_body, suite_body])}}}
        real_path, suite_path = os.path.join(self.tmp, "real.json"), os.path.join(self.tmp, "suite.json")
        for path, data in ((real_path, real), (suite_path, suite)):
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)

        out = self.scan(root, "--lines", real_path, "--lines", suite_path)
        # ran_live's body ran in a real run: dropped. Def and assignment lines run at import and veto nothing.
        self.assertEqual(self.names_in(out, rel), {"ran_suite_only", "never_ran", "CONST_A"})
        self.assertIs(self.one(out, rel, "ran_suite_only")["ran_in_suite"], True)
        self.assertIs(self.one(out, rel, "never_ran")["ran_in_suite"], False)

        # Suite data alone vetoes nothing: every candidate stays, marked.
        out2 = self.scan(root, "--lines", suite_path)
        self.assertEqual(self.names_in(out2, rel), {"ran_live", "ran_suite_only", "never_ran", "CONST_A"})
        self.assertIs(self.one(out2, rel, "ran_live")["ran_in_suite"], True)
        self.assertIs(self.one(out2, rel, "never_ran")["ran_in_suite"], False)


# ====================================================================== today's Harness
class RealTree(ScanCase):
    def test_todays_harness(self):
        """The note's five measured dead candidates, and none of its false positives."""
        out = self.scan(ROOT)
        cands = out["candidates"]
        file_runners = "FILE_" + "RUNNERS"  # spelled apart so this test file does not reference it
        expected = (  # path, name, kind, refs, a test file that must be listed
            (LIB + "programs.py", file_runners, "constant", "none", None),
            (LIB + "backups.py", "store_dir", "function", "test+contract", T + "test_backups.py"),
            (LIB + "routing.py", "KEYS", "constant", "test+contract", T + "test_harness_lib_routing.py"),
            (LIB + "waves.py", "has_wave_review", "function", "test+contract", T + "test_harness_lib_waves.py"),
            (BIN + "harness-pr", "--yes", "flag", "test+contract", T + "test_pr_land.py"),
        )
        for path, name, kind, refs, test in expected:
            c = self.one(out, path, name)
            self.assertEqual((c["kind"], c["refs"]), (kind, refs), c)
            if test is None:
                self.assertEqual(c["tests"], [], c)
            else:
                self.assertIn(test, c["tests"], c)

        with open(os.path.join(ROOT, HOOKS, "hooks.json"), encoding="utf-8") as fh:
            wired = set(re.findall(r"hooks/([a-z_]+\.py)", fh.read()))
        bins = set(os.listdir(os.path.join(ROOT, BIN)))
        for c in cands:
            where = (c["path"], c["kind"], c["name"])
            self.assertFalse(c["path"].startswith((T, P + "templates/")), where)
            self.assertNotIn(c["path"], {HOOKS + "land_ask.py", HOOKS + "session_brief.py"}, where)
            base = os.path.basename(c["path"])
            if c["path"].startswith(BIN):  # a bin tool is never a candidate; its dead flags or helpers may be
                self.assertNotIn(last(c["name"]), bins | {"main"}, where)
                self.assertNotEqual(c["kind"], "module", where)
            if c["path"].startswith(HOOKS) and base in wired:
                self.assertNotEqual(c["kind"], "module", where)
                self.assertNotIn(last(c["name"]), {base, base[:-3], "main"}, where)
        not_dead = {
            (BIN + "harness-board", "close"), (BIN + "harness-board", "epic-check"),
            (BIN + "harness-worklog", "--rewrite-old"), (BIN + "harness-worklog", "--gap-minutes"),
            (BIN + "harness-worklog", "--drop-stale"), (BIN + "harness-worklog", "--ignore-commit"),
            (BIN + "harness-worklog", "--evidence"),
            (LIB + "repo.py", "locked"), (LIB + "repo.py", "prunable"), (LIB + "cli.py", "error"),
            (HOOKS + "verify_on_stop.py", "on_signal"), (HOOKS + "backup_gate.py", "out_of_time"),
            (HOOKS + "backup_gate.py", "secrets"),
            (LIB + "programs.py", "preload_word"), (LIB + "programs.py", "wrapper_subcommand"),
            (LIB + "programs.py", "wrapper_programs"), (LIB + "programs.py", "deno_programs"),
            (LIB + "programs.py", "bun_programs"), (BIN + "harness-worklog", "first_day"),
        }
        self.assertEqual({(c["path"], last(c["name"])) for c in cands} & not_dead, set())
        self.assertFalse({"required", "frame", "signum"} & {last(c["name"]) for c in cands}, cands)


# ====================================================================== standard library only
class StandardLibrary(unittest.TestCase):
    def test_the_tool_is_standard_library_only_and_parses_as_39(self):
        self.assertTrue(os.path.isfile(TOOL), f"{TOOL} does not exist")
        with open(TOOL, encoding="utf-8") as fh:
            text = fh.read()
        tree = ast.parse(text, filename=TOOL, feature_version=(3, 9))
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                names = [node.module or ""]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top not in ("__future__", "harness_lib") and not is_stdlib(top):
                    bad.append(f"line {node.lineno}: {name}")
        self.assertEqual(bad, [])
        self.assertTrue(text.startswith("#!/usr/bin/env python3"))


if __name__ == "__main__":
    unittest.main()
