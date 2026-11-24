"""Tests for #474: scripts/collect_lines.py, which collects the lines of Harness that run under the suite and under a
loop smoke, subprocesses included, with a standard-library tracer started from a `.pth` in a bare venv.

    python3 -m unittest discover -s plugins/harness/tests -p test_collect_lines.py

No real suite and no real loop smoke run here. Each test builds a small fixture checkout (a git repository with a copy
of scripts/collect_lines.py, of harness_lib, and stand-in `scripts/run_suite.py` and `scripts/loop_smoke.py`), so the
script finds the checkout from its own location and wraps the stand-ins instead of the real runners. The stand-in
runner starts the kinds of process the real suite starts: a hook run by `sys.executable`, a bin tool run by name
(`#!/usr/bin/env python3`) with an environment rebuilt to HOME (another folder), PATH, TMPDIR and LANG, a tool and a
scripts/ module run as `python3` from PATH, a thread, a test file under plugins/harness/tests/, a file in the checkout
outside plugins/harness and scripts, a file outside the checkout, and a process ended by SIGTERM. It records each
child's exit status and stdout to $FIXTURE_REPORT. HOME is a temporary folder per test, so the venv
(~/.claude/harness/deadcode/venv) and the state folder land under it.

The merged line data is found by structure, not by path: a JSON file under ~/.claude/harness/deadcode (outside the
venv) whose top level holds `files` and `real_run`. Its `files` maps a path relative to the checkout to
{"executed_lines": [statement lines]}. Expected lines are read from `# @tag` markers in the fixture sources.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SCRIPT = os.path.join(REPO, "scripts", "collect_lines.py")
IDENTITY = dict(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
RUN_TIMEOUT = 180

FAKE_RUN_SUITE = r'''
import json, os, signal, subprocess, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugins", "harness")
TOOL = os.path.join(PLUGIN, "bin", "harness-fake")
mode = os.environ.get("FIXTURE_MODE", "spread")
report = []


def run(name, argv, stdin="", env=None):
    proc = subprocess.run(argv, input=stdin, capture_output=True, text=True, env=env, timeout=60)
    report.append({"name": name, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr})


if mode == "spread":
    run("hook", [sys.executable, os.path.join(PLUGIN, "hooks", "fakehook.py")], stdin='{"x": 1}')
    rebuilt = {k: os.environ[k] for k in ("PATH", "TMPDIR", "LANG") if k in os.environ}
    rebuilt["HOME"] = os.environ["FIXTURE_OTHER_HOME"]
    run("tool-alpha", [TOOL, "alpha"], env=rebuilt)
    run("tool-beta", ["python3", TOOL, "beta"])
    run("test", ["python3", os.path.join(PLUGIN, "tests", "test_fake.py")])
    run("outside", ["python3", os.environ["FIXTURE_OUTSIDE"]])
    run("user", ["python3", os.path.join(ROOT, "user", "notplugin.py")])
    run("helper", ["python3", os.path.join(ROOT, "scripts", "helper.py")])
    ready = os.path.join(os.environ["FIXTURE_OTHER_HOME"], "slow-ready")
    child = subprocess.Popen(["python3", os.path.join(PLUGIN, "hooks", "slow.py"), ready],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 30
    while not os.path.exists(ready) and time.time() < deadline:
        time.sleep(0.05)
    child.send_signal(signal.SIGTERM)
    report.append({"name": "slow", "returncode": child.wait(timeout=30), "ready": os.path.exists(ready)})
elif mode == "beta-only":
    run("tool-beta", ["python3", TOOL, "beta"])
with open(os.environ["FIXTURE_REPORT"], "w", encoding="utf-8") as fh:
    json.dump(report, fh)
sys.exit(int(os.environ.get("FIXTURE_EXIT", "0")))
'''

FAKE_LOOP_SMOKE = r'''
import json, os, subprocess, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.environ["FIXTURE_ARGV"], "w", encoding="utf-8") as fh:
    json.dump(sys.argv[1:], fh)
subprocess.run(["python3", os.path.join(ROOT, "plugins", "harness", "bin", "harness-fake"), "loop"])
sys.exit(int(os.environ.get("FIXTURE_EXIT", "0")))
'''

FIXTURE_FILES = {
    "plugins/harness/bin/harness-fake": '''\
        #!/usr/bin/env python3
        import os  # @tool
        import sys  # @tool
        import threading  # @tool

        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))  # @tool
        import sample  # @tool

        mode = sys.argv[1]  # @tool
        if mode == "alpha":  # @tool
            sample.alpha()  # @tool-alpha
            worker = threading.Thread(target=sample.threaded)  # @tool-alpha
            worker.start()  # @tool-alpha
            worker.join()  # @tool-alpha
        elif mode == "beta":
            sample.beta()  # @tool-beta
        elif mode == "loop":
            sample.delta()  # @tool-loop
        print("tool " + mode)  # @tool
        ''',
    "plugins/harness/lib/sample.py": '''\
        def alpha():  # @sample-defs
            value = 1  # @alpha
            return value  # @alpha


        def beta():  # @sample-defs
            value = 2  # @beta
            return value  # @beta


        def threaded():  # @sample-defs
            value = 3  # @thread
            return value  # @thread


        def gamma():  # @sample-defs
            value = 4  # @never
            return value  # @never


        def delta():  # @sample-defs
            value = 5  # @delta
            return value  # @delta
        ''',
    "plugins/harness/lib/mapping_fixture.py": '''\
        import os  # @map


        def deco(func):  # @map
            return func  # @map


        @deco
        def multi(  # @map
            first,
            second,
        ):
            total = (  # @map
                first
                + second
            )
            if (  # @map
                first > second
            ):
                return [  # @map
                    total,
                    os.sep,
                ]
            return total


        def unused():  # @map
            return None
        ''',
    "plugins/harness/hooks/fakehook.py": '''\
        import json  # @hook
        import sys  # @hook

        data = json.load(sys.stdin)  # @hook
        print(json.dumps({"echo": data}))  # @hook
        ''',
    "plugins/harness/hooks/slow.py": '''\
        import sys  # @slow
        import time  # @slow


        def before():  # @slow
            value = 1  # @slow
            return value  # @slow


        before()  # @slow
        open(sys.argv[1], "w").close()  # @slow
        time.sleep(30)  # @slow
        print("after")  # @slow-after
        ''',
    "plugins/harness/tests/test_fake.py": '''\
        value = 1  # @test
        print("test", value)  # @test
        ''',
    "user/notplugin.py": '''\
        value = 1  # @user
        print("user", value)  # @user
        ''',
    "scripts/helper.py": '''\
        import os  # @helper
        import sys  # @helper

        ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # @helper
        sys.path.insert(0, os.path.join(ROOT, "plugins", "harness", "lib"))  # @helper
        import mapping_fixture  # @helper

        mapping_fixture.multi(2, 1)  # @helper
        print("helper")  # @helper
        ''',
}

OUTSIDE = '''\
value = 1  # @outside
print("outside", value)  # @outside
'''


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mode:
        os.chmod(path, os.stat(path).st_mode | mode)


def git(env, *args):
    p = subprocess.run(["git", *args], env=env, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {p.stderr}")


def build(base):
    """A fixture world under `base`: a checkout (a git repository with one commit), a test HOME, another HOME for the
    rebuilt-environment child, and a file outside the checkout. Returns a dict of the paths."""
    if not os.path.isfile(SCRIPT):
        raise AssertionError(f"{SCRIPT} does not exist")
    root = os.path.join(base, "checkout")
    shutil.copytree(os.path.join(REPO, "plugins", "harness", "lib"), os.path.join(root, "plugins", "harness", "lib"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    os.makedirs(os.path.join(root, "scripts"))
    shutil.copyfile(SCRIPT, os.path.join(root, "scripts", "collect_lines.py"))
    write(os.path.join(root, "scripts", "run_suite.py"), FAKE_RUN_SUITE)
    write(os.path.join(root, "scripts", "loop_smoke.py"), FAKE_LOOP_SMOKE)
    for rel, text in FIXTURE_FILES.items():
        mode = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH if "/bin/" in rel else None
        write(os.path.join(root, *rel.split("/")), textwrap.dedent(text), mode)
    world = {"base": base, "root": root, "home": os.path.join(base, "home"),
             "other_home": os.path.join(base, "other-home"), "outside": os.path.join(base, "outside", "outside.py"),
             "report": os.path.join(base, "report.json"), "argv": os.path.join(base, "argv.json"),
             "git_env": dict(os.environ, HOME=base, GIT_CONFIG_NOSYSTEM="1", **IDENTITY)}
    os.makedirs(world["home"])
    os.makedirs(world["other_home"])
    write(world["outside"], OUTSIDE)
    env = world["git_env"]
    git(env, "-C", root, "init", "-q")
    git(env, "-C", root, "symbolic-ref", "HEAD", "refs/heads/main")
    git(env, "-C", root, "config", "gc.auto", "0")
    git(env, "-C", root, "add", "-A")
    git(env, "-C", root, "commit", "-q", "-m", "fixture")
    return world


def collect(world, *args, root=None, mode="spread", exit_code=0):
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.update(HOME=world["home"], FIXTURE_MODE=mode, FIXTURE_EXIT=str(exit_code), FIXTURE_REPORT=world["report"],
               FIXTURE_OUTSIDE=world["outside"], FIXTURE_OTHER_HOME=world["other_home"], FIXTURE_ARGV=world["argv"])
    script = os.path.join(root or world["root"], "scripts", "collect_lines.py")
    return subprocess.run([sys.executable, script, *args], cwd=world["base"], env=env, capture_output=True,
                          text=True, timeout=RUN_TIMEOUT)


def deadcode(world):
    return os.path.join(world["home"], ".claude", "harness", "deadcode")


def venv(world):
    return os.path.join(deadcode(world), "venv")


def merged(world):
    """{path: data} of every merged line-data JSON under the state folder (outside the venv)."""
    found = {}
    for top, dirs, files in os.walk(deadcode(world)):
        if os.path.abspath(top) == os.path.abspath(deadcode(world)) and "venv" in dirs:
            dirs.remove("venv")
        for name in files:
            if not name.endswith(".json"):
                continue
            path = os.path.join(top, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and "files" in data and "real_run" in data:
                found[path] = data
    return found


def newest(world, real_run):
    runs = [(os.path.getmtime(p), p, d) for p, d in merged(world).items() if d["real_run"] is real_run]
    if not runs:
        raise AssertionError(f"no merged JSON with real_run={real_run} under {deadcode(world)}")
    return max(runs)[2]


def state_files(world):
    """Every file under the state folder outside the venv, with its size and mtime."""
    out = set()
    for top, dirs, files in os.walk(deadcode(world)):
        if os.path.abspath(top) == os.path.abspath(deadcode(world)) and "venv" in dirs:
            dirs.remove("venv")
        for name in files:
            path = os.path.join(top, name)
            st = os.stat(path)
            out.add((path, st.st_size, st.st_mtime_ns))
    return out


def marked(world, rel, tag, root=None):
    path = os.path.join(root or world["root"], *rel.split("/"))
    with open(path, encoding="utf-8") as fh:
        return {n for n, line in enumerate(fh, 1) if line.rstrip().endswith("# @" + tag)}


def lines(data, rel):
    entry = data["files"].get(rel)
    if entry is None:
        return set()
    return set(entry["executed_lines"])


def run_without_collection(world):
    """Run a hook and a tool of the fixture with the venv's python3 first on PATH, after the collection is over.
    Returns the hook's stdout."""
    env = dict(os.environ, HOME=world["home"],
               PATH=os.path.join(venv(world), "bin") + os.pathsep + os.environ.get("PATH", ""))
    env.pop("PYTHONHOME", None)
    py = os.path.join(venv(world), "bin", "python3")
    hook = subprocess.run([py, os.path.join(world["root"], "plugins", "harness", "hooks", "fakehook.py")],
                          input='{"y": 2}', capture_output=True, text=True, env=env, timeout=60)
    tool = subprocess.run([os.path.join(world["root"], "plugins", "harness", "bin", "harness-fake"), "alpha"],
                          capture_output=True, text=True, env=env, timeout=60)
    return hook, tool


class SuiteRunTest(unittest.TestCase):
    """One `collect_lines.py suite` over the spread stand-in runner, shared by every assertion below."""

    @classmethod
    def setUpClass(cls):
        cls.base = os.path.realpath(tempfile.mkdtemp(prefix="harness-collect-lines-"))
        cls.world = build(cls.base)
        cls.started = time.time()
        cls.proc = collect(cls.world, "suite")
        with open(cls.world["report"], encoding="utf-8") as fh:
            cls.report = {r["name"]: r for r in json.load(fh)}
        found = [d for d in merged(cls.world).values() if d["real_run"] is False]
        cls.data = found[0] if len(found) == 1 else None
        cls.found = found

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, True)

    def setUp(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stdout + self.proc.stderr)
        self.assertEqual(len(self.found), 1, f"expected one merged suite JSON, found {len(self.found)}")

    def test_writes_one_suite_json_not_marked_real(self):
        self.assertIs(self.data["real_run"], False)
        self.assertIsInstance(self.data["files"], dict)
        for rel, entry in self.data["files"].items():
            self.assertFalse(os.path.isabs(rel), rel)
            self.assertTrue(rel.startswith(("plugins/harness/", "scripts/")), rel)
            got = entry["executed_lines"]
            self.assertTrue(all(isinstance(n, int) for n in got), rel)
            self.assertEqual(got, sorted(set(got)), f"{rel}: lines not sorted and unique")

    def test_hook_tool_thread_and_scripts_processes_are_recorded(self):
        w, d = self.world, self.data
        cases = [
            ("plugins/harness/hooks/fakehook.py", "hook"),  # run by sys.executable
            ("plugins/harness/bin/harness-fake", "tool"),
            ("plugins/harness/bin/harness-fake", "tool-alpha"),  # by name, environment rebuilt, HOME elsewhere
            ("plugins/harness/bin/harness-fake", "tool-beta"),  # python3 from PATH
            ("plugins/harness/lib/sample.py", "alpha"),
            ("plugins/harness/lib/sample.py", "thread"),  # a thread of the rebuilt-environment process
            ("scripts/helper.py", "helper"),
        ]
        for rel, tag in cases:
            with self.subTest(rel=rel, tag=tag):
                want = marked(w, rel, tag)
                self.assertTrue(want)
                self.assertLessEqual(want, lines(d, rel))
        for rel, tag in [("plugins/harness/bin/harness-fake", "tool-loop"), ("plugins/harness/lib/sample.py", "never"),
                         ("plugins/harness/lib/sample.py", "delta")]:
            with self.subTest(rel=rel, absent=tag):
                self.assertFalse(marked(w, rel, tag) & lines(d, rel))

    def test_tests_other_checkout_folders_and_outside_files_are_left_out(self):
        for rel in self.data["files"]:
            with self.subTest(rel=rel):
                self.assertFalse(rel.startswith("plugins/harness/tests/"), rel)
                self.assertFalse(rel.startswith("user/"), rel)
                self.assertNotIn("outside.py", rel)
        for name in ("test", "outside", "user"):
            self.assertEqual(self.report[name]["returncode"], 0, self.report[name])

    def test_a_process_ended_by_sigterm_still_dumps(self):
        self.assertTrue(self.report["slow"]["ready"], "the slow child never got going")
        rel = "plugins/harness/hooks/slow.py"
        self.assertLessEqual(marked(self.world, rel, "slow"), lines(self.data, rel))
        self.assertFalse(marked(self.world, rel, "slow-after") & lines(self.data, rel))

    def test_lines_of_separate_processes_are_unioned_per_file(self):
        # alpha ran only in the rebuilt-environment process, beta only in the python3 one, threaded only in a thread
        rel = "plugins/harness/lib/sample.py"
        want = set()
        for tag in ("sample-defs", "alpha", "beta", "thread"):
            want |= marked(self.world, rel, tag)
        self.assertEqual(lines(self.data, rel), want)

    def test_continuation_and_decorator_lines_map_to_the_statement(self):
        rel = "plugins/harness/lib/mapping_fixture.py"
        self.assertEqual(sorted(lines(self.data, rel)), sorted(marked(self.world, rel, "map")))

    def test_children_run_unchanged(self):
        self.assertEqual(json.loads(self.report["hook"]["stdout"]), {"echo": {"x": 1}})
        for name in ("hook", "tool-alpha", "tool-beta", "helper"):
            with self.subTest(child=name):
                self.assertEqual(self.report[name]["returncode"], 0, self.report[name])
        self.assertEqual(self.report["tool-alpha"]["stdout"].strip(), "tool alpha")

    def test_venv_holds_only_the_pth_and_the_starter(self):
        root = venv(self.world)
        with open(os.path.join(root, "pyvenv.cfg"), encoding="utf-8") as fh:
            cfg = dict(line.split("=", 1) for line in fh.read().splitlines() if "=" in line)
        cfg = {k.strip(): v.strip() for k, v in cfg.items()}
        self.assertEqual(cfg.get("include-system-site-packages"), "false")
        sites = [os.path.join(root, "lib", name, "site-packages") for name in os.listdir(os.path.join(root, "lib"))]
        sites = [s for s in sites if os.path.isdir(s)]
        self.assertEqual(len(sites), 1, sites)
        entries = sorted(e for e in os.listdir(sites[0]) if e != "__pycache__")
        self.assertEqual(len(entries), 2, entries)
        self.assertEqual(sorted(os.path.splitext(e)[1] for e in entries), [".pth", ".py"], entries)
        self.assertFalse([b for b in os.listdir(os.path.join(root, "bin")) if b.startswith(("pip", "easy_install"))])

    def test_nothing_installed_outside_the_state_folder(self):
        home = self.world["home"]
        stray = []
        for top, dirs, files in os.walk(home):
            rel = os.path.relpath(top, home)
            if rel == ".":
                dirs[:] = [x for x in dirs if x != "Library"]
            if rel.split(os.sep)[:3] == [".claude", "harness", "deadcode"]:
                dirs[:] = []
                continue
            stray += [os.path.join(rel, f) for f in files]
        self.assertEqual(stray, [])
        root = venv(self.world)
        site_dir = [os.path.join(root, "lib", n, "site-packages") for n in os.listdir(os.path.join(root, "lib"))][0]
        starter = [os.path.splitext(e)[0] for e in os.listdir(site_dir) if e.endswith(".py")][0]
        with open(os.path.join(root, "pyvenv.cfg"), encoding="utf-8") as fh:
            home_bin = [ln.split("=", 1)[1].strip() for ln in fh if ln.split("=", 1)[0].strip() == "home"][0]
        base = os.path.join(home_bin, "python3")
        base = base if os.path.exists(base) else sys.executable
        out = subprocess.run([base, "-I", "-c", "import json, site; print(json.dumps(site.getsitepackages()))"],
                             capture_output=True, text=True, timeout=60)
        for d in json.loads(out.stdout):
            if not os.path.isdir(d):
                continue
            for name in os.listdir(d):
                with self.subTest(base_site=d, entry=name):
                    self.assertNotEqual(name, starter + ".py")
                    if name.endswith(".pth"):
                        with open(os.path.join(d, name), encoding="utf-8", errors="replace") as fh:
                            text = fh.read()
                        self.assertNotIn(starter, text)
                        self.assertNotIn("deadcode", text)

    def test_collection_is_off_after_the_run(self):
        before = state_files(self.world)
        hook, tool = run_without_collection(self.world)
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertEqual(json.loads(hook.stdout), {"echo": {"y": 2}})
        self.assertEqual(tool.returncode, 0, tool.stderr)
        self.assertEqual(state_files(self.world), before)


class RunsTest(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="harness-collect-lines-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.world = build(self.base)

    def test_loop_smoke_writes_a_second_json_marked_real(self):
        w = self.world
        suite = collect(w, "suite", mode="beta-only")
        self.assertEqual(suite.returncode, 0, suite.stdout + suite.stderr)
        self.assertFalse(os.path.exists(w["argv"]), "suite ran loop_smoke.py")
        sandbox = os.path.join(self.base, "harness-sandbox")
        os.makedirs(sandbox)
        loop = collect(w, "loop-smoke", sandbox)
        self.assertEqual(loop.returncode, 0, loop.stdout + loop.stderr)
        with open(w["argv"], encoding="utf-8") as fh:
            argv = json.load(fh)
        self.assertEqual(argv[0], "run")
        self.assertEqual(os.path.realpath(argv[argv.index("--sandbox") + 1]), sandbox)
        self.assertEqual(os.path.realpath(argv[argv.index("--plugin") + 1]),
                         os.path.join(w["root"], "plugins", "harness"))
        found = merged(w)
        self.assertEqual(sorted(d["real_run"] for d in found.values()), [False, True])
        self.assertEqual(len(set(found)), 2)
        real, suite_data = newest(w, True), newest(w, False)
        sample, tool = "plugins/harness/lib/sample.py", "plugins/harness/bin/harness-fake"
        self.assertLessEqual(marked(w, sample, "delta"), lines(real, sample))
        self.assertLessEqual(marked(w, tool, "tool-loop"), lines(real, tool))
        self.assertFalse(marked(w, sample, "beta") & lines(real, sample))
        self.assertLessEqual(marked(w, sample, "beta"), lines(suite_data, sample))
        self.assertFalse(marked(w, sample, "delta") & lines(suite_data, sample))

    def test_loop_smoke_never_runs_by_default(self):
        w = self.world
        for args in ([], ["loop-smoke"]):
            with self.subTest(args=args):
                proc = collect(w, *args, mode="beta-only")
                self.assertFalse(os.path.exists(w["argv"]), f"{args} ran loop_smoke.py")
                self.assertFalse([d for d in merged(w).values() if d["real_run"] is True])
                if args:
                    self.assertNotEqual(proc.returncode, 0)

    def test_a_failing_suite_still_writes_its_data_and_turns_collection_off(self):
        w = self.world
        proc = collect(w, "suite", mode="beta-only", exit_code=1)
        self.assertNotEqual(proc.returncode, 0)
        data = newest(w, False)
        rel = "plugins/harness/lib/sample.py"
        self.assertLessEqual(marked(w, rel, "beta"), lines(data, rel))
        before = state_files(w)
        hook, tool = run_without_collection(w)
        self.assertEqual(json.loads(hook.stdout), {"echo": {"y": 2}})
        self.assertEqual(tool.returncode, 0, tool.stderr)
        self.assertEqual(state_files(w), before)

    def test_a_second_run_from_a_worktree_reuses_the_venv_and_keeps_only_its_own_lines(self):
        w = self.world
        first = collect(w, "suite")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        marker = os.path.join(venv(w), "kept-marker")
        write(marker, "x")
        tree = os.path.join(self.base, "worktree")
        git(w["git_env"], "-C", w["root"], "worktree", "add", "-q", "-b", "t/1-other", tree)
        time.sleep(0.05)
        second = collect(w, "suite", root=tree, mode="beta-only")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertTrue(os.path.exists(marker), "the venv was rebuilt instead of reused")
        data = newest(w, False)
        rel = "plugins/harness/lib/sample.py"
        self.assertLessEqual(marked(w, rel, "beta", root=tree), lines(data, rel))
        self.assertFalse(marked(w, rel, "alpha", root=tree) & lines(data, rel))
        self.assertNotIn("plugins/harness/hooks/fakehook.py", data["files"])


if __name__ == "__main__":
    unittest.main()
