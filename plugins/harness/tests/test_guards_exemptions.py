"""branch_guard.py on Bash: the docs/agent/ and gitignored exemptions for every write form, and copies judged by their destination.

Split from test_guards.py; the fixtures are in guards_support.py.

Run: python3 -m unittest discover -s plugins/harness/tests -p "test_guards_exemptions.py" -v
"""

from __future__ import annotations

import unittest

from guards_support import *  # noqa: F401,F403


class BranchGuardBashExemptions(GuardTestCase):
    """Bash writes on main are allowed where an Edit would be: under docs/agent/ and on
    paths git ignores. Inline python and node programs that write are allowed when every
    path literal in them lies outside every protected main working tree."""

    def setUp(self) -> None:
        super().setUp()
        # under HOME, so ~/proj/... names the repo
        self.repo = self.make_repo("home/proj")
        repo = self.repo
        self.write(repo / ".gitignore", EXEMPT_GITIGNORE)
        self.write(repo / "docs/agent/handoff.md", "# Handoff\n")
        self.write(repo / "docs/design.md", "# Design\n")
        self.write(repo / "keep.log", "tracked although *.log is ignored\n")
        self.git(repo, "add", ".gitignore", "docs")
        self.git(repo, "add", "-f", "keep.log")
        self.git(repo, "commit", "-q", "-m", "Ignore local files, add notes")
        # ignored and untracked, as they sit in a working checkout
        self.write(repo / "data/a.csv", "a,b\n1,2\n")
        self.write(repo / "cache/blob", "x\n")
        self.write(repo / ".env.local", "TOKEN_FILE=x\n")
        self.write(repo / "build.log", "ok\n")
        (self.home / ".venv").mkdir()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        self.write(self.outside / "notes.md", "notes\n")
        self.write(self.outside / "fix.diff", "")
        # the tree is still clean apart from ignored files
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")

    def bash(self, command: str, cwd: Path | None = None, env=None) -> str | None:
        tool_input = {"command": command, "description": "Run it"}
        payload = self.payload("Bash", tool_input, cwd or self.repo)
        return self.decision(self.run_hook("branch_guard.py", payload, env))

    def assertAllowed(self, commands, cwd: Path | None = None) -> None:
        for command in commands:
            with self.subTest(command=command, cwd=str(cwd or self.repo)):
                self.assertIsNone(self.bash(command, cwd=cwd))

    def assertRefusedOnMain(self, reason, part: str, why: str) -> None:
        self.assertEqual(
            reason,
            f'On main in proj. Refused "{as_shown(part)}": {why}. {ADVICE}',
        )

    def assertRefusedPart(self, reason, part: str) -> None:
        """Refused on main for `part`, whatever the stated reason."""
        self.assertIsNotNone(reason)
        prefix = f'On main in proj. Refused "{as_shown(part)}": '
        self.assertTrue(reason.startswith(prefix), reason)
        self.assertTrue(reason.endswith(f". {ADVICE}"), reason)

    # ---- broad: the ticket's cases through the hook

    def test_copy_into_docs_agent_allowed(self):
        repo, outside = self.repo, self.outside
        self.assertAllowed(
            (
                "cp notes.md docs/agent/handoff.md",
                f"cp {outside}/notes.md docs/agent/handoff.md",
                f"cp {outside}/notes.md docs/agent/",
                f"cp {outside}/notes.md docs/agent/specs/2026-10-23-x.md",
                f"cp -R {outside} docs/agent/imported",
                f"cp {outside}/notes.md docs/agent/a.md && cp {outside}/notes.md docs/agent/b.md",
            )
        )
        self.assertAllowed(
            (
                f"cp notes.md {repo}/docs/agent/handoff.md",
                f"cd {repo} && cp {outside}/notes.md docs/agent/handoff.md",
            ),
            cwd=outside,
        )
        # the same copy anywhere else in the repo stays refused
        for command, part, why in (
            (
                "cp notes.md docs/design.md",
                "cp notes.md docs/design.md",
                "cp changes docs/design.md inside the repo",
            ),
            (
                "cp notes.md docs/agentx.md",
                "cp notes.md docs/agentx.md",
                "cp changes docs/agentx.md inside the repo",
            ),
            (
                "cp notes.md docs/",
                "cp notes.md docs/",
                "cp changes docs/ inside the repo",
            ),
            (
                "cp notes.md docs/agent/a.md && cp notes.md src/app.py",
                "cp notes.md src/app.py",
                "cp changes src/app.py inside the repo",
            ),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(self.bash(command), part, why)

    def test_symlink_onto_ignored_path_allowed(self):
        self.assertAllowed(
            (
                "ln -sfn ../.venv .venv",
                f"ln -s {self.home}/.venv .venv",
                f"ln -sf {self.outside}/notes.md build.log",
                f"ln -s {self.outside}/notes.md docs/agent/linked.md",
            )
        )
        for command, part, why in (
            (
                "ln -sfn ../.venv venv",
                "ln -sfn ../.venv venv",
                "ln changes venv inside the repo",
            ),
            (
                f"ln -sf {self.outside}/notes.md src/app.py",
                f"ln -sf {self.outside}/notes.md src/app.py",
                "ln changes src/app.py inside the repo",
            ),
            (
                f"ln -sf {self.outside}/notes.md keep.log",
                f"ln -sf {self.outside}/notes.md keep.log",
                "ln changes keep.log inside the repo",
            ),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(self.bash(command), part, why)

    def test_redirect_into_ignored_file_allowed(self):
        self.assertAllowed(
            (
                "echo x > .env.local",
                "echo x >> .env.local",
                "make check > build.log 2>&1",
                "make check > logs/today.log",
                "printf 'a,b\\n' > data/out.csv",
                "echo note >> docs/agent/handoff.md",
                "cd src && echo x > ../.env.local",
            )
        )
        self.assertAllowed(
            (
                f"echo x > {self.repo}/.env.local",
                f"echo x > {self.repo}/docs/agent/handoff.md",
            ),
            cwd=self.outside,
        )
        for command, target in (
            ("echo x > .env.example", ".env.example"),
            ("echo x > env.local", "env.local"),
            ("echo x > docs/design.md", "docs/design.md"),
            ("echo x > datafile.csv", "datafile.csv"),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(
                    self.bash(command),
                    command,
                    f"it writes to {target} through a redirection",
                )

    def test_heredoc_writing_only_outside_repos_allowed(self):
        repo, outside = self.repo, self.outside
        nested = repo / ".claude/worktrees/t-13-x"
        self.git(repo, "worktree", "add", "-q", "-b", "t/13-x", str(nested))
        self.assertAllowed(
            (
                # the ticket's case
                heredoc(
                    "import json\n"
                    f"with open('{OUTSIDE_JSON}', 'w') as f:\n"
                    "    json.dump({'ok': True}, f)"
                ),
                heredoc(f"open('{outside}/out.json', 'w').write('x')", "python3"),
                heredoc(
                    "from pathlib import Path\n"
                    f"Path('{outside}/report.txt').write_text('x')"
                ),
                # several literals, all outside: one read, two written
                heredoc(
                    f"src = open('{outside}/notes.md').read()\n"
                    f"open('{OUTSIDE_JSON}', 'w').write(src)\n"
                    f"open('{outside}/copy.md', 'a').write(src)"
                ),
                heredoc(
                    "import shutil\n"
                    f"shutil.copy('{outside}/notes.md', '/private/tmp/x/notes.md')"
                ),
                # ~ and ../ resolve outside the repo
                heredoc(
                    "import os\n"
                    "open(os.path.expanduser('~/elsewhere/out.json'), 'w')"
                ),
                heredoc("open('../../outside/rel.json', 'w')"),
                # a ticket worktree is judged by its own branch
                heredoc(f"open('{nested}/out.json', 'w').write('x')"),
            )
        )

    def test_inline_python_and_node_programs_are_not_read(self):
        # An inline program is not read on main: nothing it writes can be committed or pushed (the
        # git hooks), and tests_first refuses such programs for the agent roles on a ticket branch.
        outside = self.outside
        self.assertAllowed(
            (
                f"""python3 -c "open('{OUTSIDE_JSON}','w').write('x')\"""",
                f"""python3 -c "open('{outside}/a.txt', 'w')\"""",
                f"""python3 -c "import shutil; shutil.copy('{outside}/notes.md', '{outside}/b.md')\"""",
                f"""node -e "require('fs').writeFileSync('{OUTSIDE_JSON}', 'x')\"""",
                f"""node --eval="require('fs').writeFileSync('{outside}/o.txt', 'x')\"""",
                heredoc(
                    f"require('fs').writeFileSync('{OUTSIDE_JSON}', 'x')", "node", "JS"
                ),
                """python3 -c "open('./src/app.py','w').write('x')\"""",
                """node -e "require('fs').writeFileSync('./src/app.js', 'x')\"""",
                heredoc("open('src/x', 'w')"),
            )
        )

    def test_deleting_or_moving_ignored_paths_allowed(self):
        self.assertAllowed(
            (
                "rm -rf data",
                "rm data/a.csv",
                "mv data/a.csv data/b.csv",
                "rm -f .env.local build.log",
                "rm -rf cache .venv",
                "truncate -s 0 build.log",
                f"mv .env.local {self.outside}/",
                f"mv {self.outside}/notes.md data/notes.md",
                # docs/agent/, tracked or not
                "rm docs/agent/handoff.md",
                "rm -rf docs/agent/specs",
                "mv docs/agent/handoff.md docs/agent/2026-10-23-handoff.md",
                "mv data/a.csv docs/agent/a.csv",
            )
        )
        for command, why in (
            ("rm README.md", "rm changes README.md inside the repo"),
            ("rm -rf src", "rm changes src inside the repo"),
            ("rm keep.log", "rm changes keep.log inside the repo"),
            ("rm data/a.csv src/app.py", "rm changes src/app.py inside the repo"),
            ("rm -f docs/design.md", "rm changes docs/design.md inside the repo"),
            ("truncate -s 0 keep.log", "truncate changes keep.log inside the repo"),
            ("mv data/a.csv src/a.csv", "mv changes src/a.csv inside the repo"),
            ("mv README.md data/", "mv changes README.md inside the repo"),
            ("mv src/app.py docs/agent/", "mv changes src/app.py inside the repo"),
            (
                f"mv docs/design.md {self.outside}/",
                "mv changes docs/design.md inside the repo",
            ),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(self.bash(command), command, why)
        # An ignored directory that holds a force-added tracked file: deleting or moving
        # the directory deletes the tracked file, so it is refused; the untracked,
        # ignored file beside it is still free.
        repo = self.repo
        self.write(repo / "data/tracked.csv", "t\n")
        self.git(repo, "add", "-f", "data/tracked.csv")
        self.git(repo, "commit", "-q", "-m", "Track one file under data/")
        self.assertAllowed(("rm data/a.csv", "mv data/a.csv data/b.csv"))
        for command in (
            "rm -rf data",
            f"mv data {self.outside}/",
            "rm data/tracked.csv",
            "mv data/tracked.csv data/b.csv",
        ):
            with self.subTest(command=command, fixture="data/tracked.csv tracked"):
                self.assertRefusedPart(self.bash(command), command)



    def test_tracked_file_redirect_still_refused(self):
        for command, part, target in (
            ("echo x > README.md", "echo x > README.md", "README.md"),
            ("echo x >> keep.log", "echo x >> keep.log", "keep.log"),
            ("echo x > .gitignore", "echo x > .gitignore", ".gitignore"),
            ("echo x > src/app.py", "echo x > src/app.py", "src/app.py"),
            (
                "echo x > .env.local && echo y > src/app.py",
                "echo y > src/app.py",
                "src/app.py",
            ),
            (
                "echo x > docs/agent/handoff.md; echo y >> README.md",
                "echo y >> README.md",
                "README.md",
            ),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(
                    self.bash(command),
                    part,
                    f"it writes to {target} through a redirection",
                )
        target = f"{self.repo}/README.md"
        self.assertRefusedOnMain(
            self.bash(f"echo x > {target}", cwd=self.outside),
            f"echo x > {target}",
            f"it writes to {target} through a redirection",
        )

    # ---- specific: each write form, exempt target against tracked target

    def test_each_write_form_follows_the_exemptions(self):
        outside = self.outside
        for allowed, refused, part, why in (
            (
                "ls | tee build.log",
                "ls | tee README.md",
                "tee README.md",
                "tee changes README.md inside the repo",
            ),
            (
                "ls | tee -a docs/agent/handoff.md",
                "ls | tee -a src/app.py",
                "tee -a src/app.py",
                "tee changes src/app.py inside the repo",
            ),
            (
                f"install -m 644 {outside}/notes.md docs/agent/notes.md",
                f"install -m 644 {outside}/notes.md src/app.py",
                f"install -m 644 {outside}/notes.md src/app.py",
                "install changes src/app.py inside the repo",
            ),
            (
                f"rsync -a {outside}/ docs/agent/",
                f"rsync -a {outside}/ src/",
                f"rsync -a {outside}/ src/",
                "rsync changes src/ inside the repo",
            ),
            (
                "dd if=/dev/zero of=data/blob.bin count=1",
                "dd if=/dev/zero of=README.md count=1",
                "dd if=/dev/zero of=README.md count=1",
                "dd changes README.md inside the repo",
            ),
            (
                "touch .env.local docs/agent/new.md",
                "touch .env.local src/new.py",
                "touch .env.local src/new.py",
                "touch changes src/new.py inside the repo",
            ),
            (
                "chmod 600 .env.local",
                "chmod 600 src/app.py",
                "chmod 600 src/app.py",
                "chmod changes src/app.py inside the repo",
            ),
            (
                "sed -i '' 's/x/y/' .env.local",
                "sed -i '' 's/x/y/' README.md",
                "sed -i '' 's/x/y/' README.md",
                "sed -i edits files in place",
            ),
            (
                "perl -pi -e 's/x/y/' docs/agent/handoff.md",
                "perl -pi -e 's/x/y/' docs/design.md",
                "perl -pi -e 's/x/y/' docs/design.md",
                "perl -i edits files in place",
            ),
            (
                "awk -i inplace 1 build.log",
                "awk -i inplace 1 keep.log",
                "awk -i inplace 1 keep.log",
                "awk -i inplace edits files in place",
            ),
            (
                "printf '1d\\nw\\n' | ed -s docs/agent/handoff.md",
                "printf '1d\\nw\\n' | ed -s README.md",
                "ed -s README.md",
                "ed changes README.md inside the repo",
            ),
            (
                f"patch docs/agent/handoff.md {outside}/fix.diff",
                f"patch src/app.py {outside}/fix.diff",
                f"patch src/app.py {outside}/fix.diff",
                "patch changes src/app.py inside the repo",
            ),
            (
                f"cp {outside}/notes.md docs/agent/",
                f"cp {outside}/notes.md src/",
                f"cp {outside}/notes.md src/",
                "cp changes src/ inside the repo",
            ),
            # into an existing ignored directory: refused, as on main (ruling b, 2026-10-25)
            (
                f"cp {outside}/notes.md data/x.md",
                f"cp {outside}/notes.md data/",
                f"cp {outside}/notes.md data/",
                "cp changes data/ inside the repo",
            ),
        ):
            with self.subTest(command=allowed):
                self.assertIsNone(self.bash(allowed))
            with self.subTest(command=refused):
                self.assertRefusedOnMain(self.bash(refused), part, why)

    def test_exempt_writes_do_not_cover_git_on_main(self):
        for command, part, why in (
            (
                "echo x > .env.local && git commit -am x",
                "git commit -am x",
                "git commit changes main",
            ),
            (
                "cp notes.md docs/agent/handoff.md && git push",
                "git push",
                "git push without a refspec pushes main",
            ),
            (
                "rm -rf data && git reset --hard HEAD~1",
                "git reset --hard HEAD~1",
                "git reset changes main",
            ),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(self.bash(command), part, why)

    # ---- ignore rules are git's own, docs/agent/ stands alone

    def test_ignore_rules_are_gits_own(self):
        """Globs, negations, a nested .gitignore and .git/info/exclude, as git reads them;
        names the setUp rules ignored are refused once no rule ignores them."""
        repo = self.repo
        self.write(
            repo / ".gitignore", "secrets/*.pem\ndata/*\n!data/keep.csv\n/top-only.txt\n"
        )
        self.write(repo / "sub/.gitignore", "*.tmp\n")
        git_dir = Path(self.git(repo, "rev-parse", "--absolute-git-dir"))
        self.write(git_dir / "info/exclude", "scratch-*.txt\n")
        self.write(repo / "data/keep.csv", "k\n")
        self.assertAllowed(
            (
                "echo x > secrets/k.pem",
                f"cp {self.outside}/notes.md secrets/deploy.pem",
                "echo x > data/x.csv",
                "rm data/a.csv",
                "echo x > sub/a.tmp",
                "echo x > sub/deep/b.tmp",
                "echo x > scratch-1.txt",
                "touch scratch-2.txt",
                "echo x > top-only.txt",
            )
        )
        for command, target in (
            ("echo x > .env.local", ".env.local"),
            ("echo x > build.log", "build.log"),
            ("echo x > cache/blob", "cache/blob"),
            ("echo x > secrets/k.txt", "secrets/k.txt"),
            ("echo x > secrets/sub/k.pem", "secrets/sub/k.pem"),
            ("echo x > data/keep.csv", "data/keep.csv"),
            ("echo x > a.tmp", "a.tmp"),
            ("echo x > scratch.txt", "scratch.txt"),
            ("echo x > sub/top-only.txt", "sub/top-only.txt"),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(
                    self.bash(command),
                    command,
                    f"it writes to {target} through a redirection",
                )
        for command, why in (
            ("rm data/keep.csv", "rm changes data/keep.csv inside the repo"),
            ("ln -sfn ../.venv .venv", "ln changes .venv inside the repo"),
            ("rm -rf cache", "rm changes cache inside the repo"),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(self.bash(command), command, why)

    def test_docs_agent_allowed_without_any_ignore_rules(self):
        repo, outside = self.repo, self.outside
        (repo / ".gitignore").unlink()
        git_dir = Path(self.git(repo, "rev-parse", "--absolute-git-dir"))
        self.write(git_dir / "info/exclude", "")
        self.assertAllowed(
            (
                "cp notes.md docs/agent/handoff.md",
                "echo x >> docs/agent/new.md",
                "touch docs/agent/specs/a.md",
                "rm docs/agent/handoff.md",
                f"mv {outside}/notes.md docs/agent/n.md",
            )
        )
        for command, why in (
            ("echo x > .env.local", "it writes to .env.local through a redirection"),
            ("echo x > build.log", "it writes to build.log through a redirection"),
            ("rm -rf data", "rm changes data inside the repo"),
        ):
            with self.subTest(command=command):
                self.assertRefusedOnMain(self.bash(command), command, why)

    # ---- inline programs: every literal, every protected main tree

    def assertRefusedIn(self, reason, part: str, names=("proj",)) -> None:
        """Refused on main for `part`, in one of the repos `names`, whatever the reason."""
        self.assertIsNotNone(reason)
        alternatives = "|".join(re.escape(n) for n in names)
        pattern = (
            f"On main in ({alternatives})\\. Refused \"{re.escape(as_shown(part))}\": "
            f".+\\. {re.escape(ADVICE)}"
        )
        self.assertRegex(reason, re.compile(f"^{pattern}$", re.S))


    # ---- fix round 1: what a write word reaches, not how it is spelled

    def test_glob_words_are_never_exempt(self):
        """The shell expands a glob before the command runs, so the word git is asked
        about is not the file written: `*.log` reaches the tracked keep.log."""
        for command in (
            "rm -f *.log",
            "truncate -s 0 *.log",
            "rm -rf cach?",
            "rm -rf data/[ab].csv",
            "rm -f {build,keep}.log",
            "echo x > *.log",
            "rm -f build.log *.log",
            "touch .env.local data/*.csv",
        ):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), command)
        # the same targets spelled out stay free
        self.assertAllowed(("rm -f build.log", "rm -rf cache", "rm -rf data/a.csv"))

    def test_dot_git_is_never_exempt(self):
        """A broad rule such as `.*` matches `.git` itself; writes under .git stay refused."""
        repo = self.repo
        self.write(repo / ".gitignore", ".*\n!.gitignore\n")
        for command in (
            "echo x > .git/HEAD",
            "rm -rf .git",
            "touch .git/info/x",
            "rm -rf .git/hooks",
            "echo x > .env.local && rm -rf .git",
        ):
            with self.subTest(command=command):
                reason = self.bash(command)
                self.assertIsNotNone(reason)
                self.assertTrue(reason.startswith("On main in proj. Refused "), reason)
                self.assertTrue(reason.endswith(f". {ADVICE}"), reason)
                self.assertIn(".git", reason)
        self.assertAllowed(
            ("echo x > .env.local", "echo x > .other-local", "rm -f .env.local")
        )

    def test_ignored_symlink_to_a_tracked_file_is_not_exempt(self):
        """The write lands where the symlink points: an ignored or docs/agent/ name that
        links to a tracked file is judged by that file."""
        repo = self.repo
        (repo / ".env.local").unlink()
        os.symlink("README.md", repo / ".env.local")
        os.symlink("../../README.md", repo / "docs/agent/x.md")
        os.symlink("../src/app.py", repo / "cache/app")
        os.symlink("keep.log", repo / "other.log")  # keep.log is ignored by name, tracked
        os.symlink("build.log", repo / "alias.log")  # an ignored, untracked target
        os.symlink(str(self.home / ".venv"), repo / ".venv")  # points outside the repo
        for command in (
            "echo x >> .env.local",
            "echo x > .env.local",
            "echo x > docs/agent/x.md",
            "echo x >> cache/app",
            f"cp {self.outside}/notes.md cache/app",
            "truncate -s 0 other.log",
            "echo x > other.log",
        ):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), command)
        self.assertAllowed(
            (
                "echo x > alias.log",
                "rm .venv",
                "ln -sfn ../.venv .venv",
                "echo note >> docs/agent/handoff.md",
            )
        )



    # ---- fix round 2: expanding sources into a directory, run-time joins


    # ---- fix round 3: a copy into a directory is judged by that directory

    def test_copy_into_a_directory_is_judged_by_the_directory(self):
        """BSD `cp -R src/ dest` copies the contents of src into dest, so `dest/src` is not
        what is written. Ruling (b), 2026-10-25: a cp, rsync, install or ln into an
        existing directory is exempt only when that directory is under docs/agent/; into
        an ignored directory it is refused, as on main. Named destinations keep the
        ignored-path rule."""
        repo, outside = self.repo, self.outside
        # the scenario is real: data/ and .venv/ hold a README.md that would land on the
        # tracked README.md at the repo root
        self.write(outside / "data/README.md", "clobbered\n")
        self.write(outside / ".venv/README.md", "clobbered\n")
        self.write(outside / "x.log", "from outside\n")
        self.write(outside / ".env.local", "from outside\n")
        for name in ("a.log", "b.log"):
            self.write(outside / name, "from outside\n")
        for command in (
            f"cp -R {outside}/data/ .",
            f"cp -R {outside}/.venv/ .",
            f"rsync -a {outside}/data/ .",
            f"cp {outside}/x.log .",
            f"cp {outside}/x.log ./",
            f"install {outside}/x.log .",
            f"ln -s {outside}/x.log .",
            f"cp {outside}/notes.md src/",
            # the source basename is ignored at the destination (*.log, data/, .env.local),
            # but the destination directory is tracked
            f"cp {outside}/x.log src/",
            f"cp {outside}/x.log src",
            f"cp {outside}/.env.local .",
            f"cp -R {outside}/data/ docs/",
            f"cp -R {outside}/data .",
            f"rsync -a {outside}/data/ src/",
            f"install -m 644 {outside}/x.log docs/",
            f"ln -sf {outside}/x.log tests/",
            f"cp {outside}/{{a,b}}.log .",
            # the destination is an existing ignored directory: refused, as on main
            f"cp {outside}/x.log data/",
            f"cp {outside}/{{a,b}}.log cache/",
            f"cp {outside}/x.log data",
            f"install {outside}/x.log cache/",
            f"ln -s {outside}/x.log data/",
            f"cp -t cache {outside}/x.log",
            f"cp {outside}/x.log {outside}/a.log cache/",
            f"rsync -t {outside}/x.log cache/",
        ):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), command)
        # an exempt copy beside one into the root: the root copy is the part refused
        self.assertRefusedPart(
            self.bash(f"cp {outside}/x.log docs/agent/ && cp {outside}/x.log ."),
            f"cp {outside}/x.log .",
        )
        # an exempt named copy beside one into an ignored directory
        self.assertRefusedPart(
            self.bash(f"cp {outside}/x.log build.log && cp {outside}/x.log data/"),
            f"cp {outside}/x.log data/",
        )
        # the repo root or an ignored directory named absolutely, from outside the repo
        for command in (
            f"cp -R {outside}/data/ {repo}",
            f"cp {outside}/x.log {repo}/",
            f"cp {outside}/x.log {repo}/data",
            f"cp {outside}/x.log {repo}/cache/",
        ):
            with self.subTest(command=command, cwd=str(outside)):
                self.assertRefusedPart(self.bash(command, cwd=outside), command)
        # the destination directory is under docs/agent/: every copy lands inside it
        self.assertAllowed(
            (
                f"cp {outside}/notes.md docs/agent/",
                f"cp -R {outside}/data/ docs/agent/specs/",
                f"rsync -a {outside}/ docs/agent/imported/",
                f"cp -R {outside}/data/ docs/agent/",
                f"cp {outside}/x.log docs/agent",
                f"install {outside}/x.log docs/agent/",
                f"ln -s {outside}/x.log docs/agent/",
                f"cp -t docs/agent {outside}/x.log",
                f"rsync -a {outside}/data/ docs/agent/",
            )
        )
        self.assertAllowed(
            (
                f"cp -R {outside}/data/ {repo}/docs/agent/",
                f"cp {outside}/x.log {repo}/docs/agent",
            ),
            cwd=outside,
        )
        # named file destinations are judged by the name, as before, ignored ones included
        self.assertAllowed(
            (
                "cp notes.md docs/agent/handoff.md",
                "ln -sfn ../.venv .venv",
                f"cp {outside}/x.log build.log",
                f"cp {outside}/x.log data/new.log",
                f"cp {outside}/x.log cache/blob",
                f"install -m 600 {outside}/x.log .env.local",
                f"ln -sf {outside}/x.log cache/x.log",
            )
        )

    # ---- fix round 4: recursive copies into exempt directories, the destination's repo

    def workspace_fixture(self) -> Path:
        """node_modules/ ignored, holding @scope/pkg: a workspace symlink to the tracked
        packages/pkg. Returns a restore tree outside the repo shaped like node_modules/."""
        repo = self.repo
        self.write(repo / ".gitignore", EXEMPT_GITIGNORE + "node_modules/\n")
        self.write(repo / "packages/pkg/index.js", "module.exports = 1;\n")
        self.git(repo, "add", ".gitignore", "packages")
        self.git(repo, "commit", "-q", "-m", "A workspace package")
        (repo / "node_modules/@scope").mkdir(parents=True)
        os.symlink("../../packages/pkg", repo / "node_modules/@scope/pkg")
        self.assertEqual(
            os.path.realpath(repo / "node_modules/@scope/pkg/index.js"),
            str(repo / "packages/pkg/index.js"),
        )
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")
        restore = self.outside / "restore"
        self.write(restore / "node_modules/@scope/pkg/index.js", "clobbered\n")
        self.write(self.outside / "data/README.md", "from outside\n")
        self.write(self.outside / "x.log", "from outside\n")
        for name in ("a.log", "b.log"):
            self.write(self.outside / name, "from outside\n")
        return restore



    def test_copy_into_a_directory_lands_in_the_destination_directorys_repo(self):
        """Ruling 2026-10-24: the repo a copy into a directory lands in is judged by the
        destination directory, not by dest/<basename of the source>. `cp -R w/worktrees/
        .claude/` writes .claude/ of the main checkout; `cp -R x/link/ .` writes the repo
        root though the tracked `link` points outside every repo."""
        repo, outside = self.repo, self.outside
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.write(repo / ".claude/settings.json", "{}\n")
        self.write(repo / ".gitignore", EXEMPT_GITIGNORE + ".claude/worktrees/\n")
        os.symlink(str(elsewhere), repo / "link")
        self.git(repo, "add", ".claude", ".gitignore", "link")
        self.git(repo, "commit", "-q", "-m", "Settings and a link outside")
        nested = repo / ".claude/worktrees/t-13-x"
        self.git(repo, "worktree", "add", "-q", "-b", "t/13-x", str(nested))
        linked = self.worktree(repo, "t/14-y", "wt14")
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")
        # the sources really would clobber tracked files at the destination
        self.write(outside / "worktrees/settings.json", '{"clobbered": true}\n')
        self.write(outside / "link/README.md", "clobbered\n")
        self.write(outside / "stuff/README.md", "from outside\n")
        self.write(outside / "t-13-x/README.md", "from outside\n")
        for command in (
            # the contents land in .claude/worktrees/ of the main checkout, not in the
            # ticket worktree t-13-x that dest/<basename> names
            f"cp -R {outside}/t-13-x/ .claude/worktrees/",
            f"cp -a {outside}/t-13-x/ {repo}/.claude/worktrees/",
            f"cp -R {outside}/worktrees/ .claude/",
            f"cp -a {outside}/worktrees/ .claude/",
            f"cp -R {outside}/worktrees/ .claude",
            f"cp -R {outside}/worktrees .claude/",
            f"cp -Rf {outside}/worktrees/ ./.claude/",
            f"cp -R {outside}/link/ .",
            f"cp -R {outside}/link/ ./",
            f"cp -a {outside}/link .",
            f"cp -R {outside}/link/ {repo}",
            f"ln -sf {outside}/link .",
        ):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), command)
        for command in (
            f"cp -R {outside}/worktrees/ {repo}/.claude/",
            f"cp -R {outside}/link/ {repo}",
            f"cp -a {outside}/link/ {repo}/",
        ):
            with self.subTest(command=command, cwd=str(outside)):
                self.assertRefusedPart(self.bash(command, cwd=outside), command)
        command = f"cd {repo} && cp -R {outside}/worktrees/ .claude/"
        self.assertRefusedPart(
            self.bash(command, cwd=outside), f"cp -R {outside}/worktrees/ .claude/"
        )
        # the destination directory really is a ticket worktree or outside every repo
        self.assertAllowed(
            (
                f"cp -R {outside}/worktrees/ .claude/worktrees/t-13-x/",
                f"cp -R {outside}/stuff/ .claude/worktrees/t-13-x/docs/",
                f"cp -R {outside}/stuff/ {linked}/",
                f"rsync -a {outside}/stuff/ {linked}/src/",
                f"cp -R {outside}/stuff/ link/",
                f"cp -R {outside}/link/ {outside}/copy/",
                f"cp -R {outside}/link/ {elsewhere}/",
            )
        )
        self.assertAllowed(
            (
                f"cp -R {outside}/link/ {linked}/",
                f"cp -R {outside}/worktrees/ {nested}/",
                f"cp -R {outside}/stuff/ {elsewhere}/",
            ),
            cwd=outside,
        )

    def test_copy_into_docs_agent_onto_a_symlink_out_of_it_refused(self):
        """Ruling 2026-10-24, kept for docs/agent/ by ruling (b): a cp, install, ln, rsync
        or mv into docs/agent/ is refused when dest/<basename of a source> already exists
        and resolves outside the exemption (BSD cp writes through docs/agent/x.md into the
        tracked README.md). A name that does not exist yet, a plain file there, or a link
        that stays inside the exemption keeps the directory rule. Into an ignored
        directory every copy is refused (ruling b), symlink or not."""
        repo, outside = self.repo, self.outside
        os.symlink("../src/app.py", repo / "cache/app.py")
        os.symlink("../README.md", repo / "data/README.md")
        os.symlink("../keep.log", repo / "data/keep.log")  # tracked, ignored by name
        os.symlink("../../README.md", repo / "docs/agent/x.md")
        (repo / "docs/agent/specs").mkdir()
        os.symlink("../../../src/app.py", repo / "docs/agent/specs/app.py")
        os.symlink("../../keep.log", repo / "docs/agent/keep.log")
        os.symlink("../build.log", repo / "cache/alias.log")  # ignored and untracked
        os.symlink("handoff.md", repo / "docs/agent/alias.md")  # stays in docs/agent/
        os.symlink("../../build.log", repo / "docs/agent/build.log")  # to an ignored file
        self.assertEqual(
            os.path.realpath(repo / "cache/app.py"), str(repo / "src/app.py")
        )
        self.assertEqual(
            os.path.realpath(repo / "docs/agent/specs/app.py"), str(repo / "src/app.py")
        )
        for name in (
            "app.py", "README.md", "keep.log", "x.md", "x.log", "new.py",
            "blob", "a.csv", "alias.log", "handoff.md", "alias.md", "build.log",
        ):
            self.write(outside / name, "from outside\n")
        for command in (
            # docs/agent/: the name lands on a link out of the exemption
            f"cp {outside}/x.md docs/agent/",
            f"cp -f {outside}/x.md docs/agent",
            f"mv {outside}/x.md docs/agent/",
            f"install {outside}/x.md docs/agent/",
            f"ln -sf {outside}/x.md docs/agent/",
            f"rsync -t {outside}/x.md docs/agent/",
            f"cp {outside}/keep.log docs/agent/",
            f"cp {outside}/app.py docs/agent/specs/",
            f"install -m 644 {outside}/app.py docs/agent/specs",
            # one source of several reaches a tracked file
            f"cp {outside}/new.py {outside}/x.md docs/agent/",
            f"cp {outside}/x.log {outside}/app.py docs/agent/specs/",
        ):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), command)
        for command in (
            f"cp {outside}/x.md {repo}/docs/agent/",
            f"install {outside}/app.py {repo}/docs/agent/specs/",
        ):
            with self.subTest(command=command, cwd=str(outside)):
                self.assertRefusedPart(self.bash(command, cwd=outside), command)
        # docs/agent/: absent names, plain files, links that stay in the exemption
        self.assertAllowed(
            (
                f"cp {outside}/handoff.md docs/agent/",
                f"cp {outside}/new.py docs/agent/",
                f"cp {outside}/new.py docs/agent/specs/",
                f"cp {outside}/blob {outside}/new.py docs/agent/",
                f"install {outside}/blob docs/agent/",
                f"ln -sf {outside}/blob docs/agent/",
                f"rsync -t {outside}/a.csv docs/agent/",
                f"mv {outside}/new.py docs/agent/specs/",
                f"cp {outside}/alias.md docs/agent/",
                f"cp {outside}/build.log docs/agent/",
            )
        )
        self.assertAllowed(
            (
                f"cp {outside}/blob {repo}/docs/agent/",
                f"cp {outside}/new.py {repo}/docs/agent/specs/",
            ),
            cwd=outside,
        )
        # into an ignored directory: refused whether the name is a link out, a link that
        # stays ignored, a plain file or absent
        for command in (
            f"cp {outside}/app.py cache/",
            f"cp -f {outside}/app.py cache",
            f"install {outside}/app.py cache/",
            f"install -m 644 {outside}/README.md data/",
            f"ln -sf {outside}/app.py cache/",
            f"rsync -t {outside}/app.py cache/",
            f"mv {outside}/app.py cache/",
            f"cp {outside}/keep.log data/",
            # one source of several reaches a tracked file
            f"cp {outside}/x.log {outside}/README.md data/",
            f"cp {outside}/blob {outside}/app.py cache/",
            f"cp {outside}/new.py cache/",
            f"cp {outside}/blob cache/",
            f"cp -f {outside}/a.csv data/",
            f"cp {outside}/blob {outside}/new.py cache/",
            f"install {outside}/blob cache/",
            f"ln -sf {outside}/blob cache/",
            f"rsync -t {outside}/a.csv data/",
            f"cp {outside}/alias.log cache/",
        ):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), command)
        for command in (
            f"cp {outside}/app.py {repo}/cache/",
            f"install {outside}/README.md {repo}/data/",
            f"cp {outside}/blob {repo}/cache/",
            f"cp {outside}/new.py {repo}/data/",
        ):
            with self.subTest(command=command, cwd=str(outside)):
                self.assertRefusedPart(self.bash(command, cwd=outside), command)

    def test_recursive_copy_to_a_new_name_keeps_the_named_rule(self):
        """A recursive copy to a name that does not exist yet creates it; it is a named
        destination, not a copy into a directory, and keeps the name's rule."""
        outside = self.outside
        self.write(outside / "data/README.md", "from outside\n")
        self.assertFalse((self.repo / "cache/restored").exists())
        self.assertAllowed(
            (
                f"cp -R {outside}/data cache/restored",
                f"cp -a {outside}/data data/copy",
            )
        )
        for command in (f"cp -R {outside}/data src/restored", f"cp -a {outside}/data newdir"):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), command)

    # ---- fix round 5, ruling (b): holder() on both paths, expanding sources, ln -n

    def test_copy_into_an_outside_symlink_to_a_protected_directory_refused(self):
        """holder() judges where a copy into a directory lands, the directory resolved
        and dest/<basename>: with ~/h -> <main checkout>/src, `cp evil.py ~/h/` writes the
        tracked src/ and is refused, as on main; so is a link to the ignored cache/ (a
        copy into an ignored directory, ruling b). A link to a directory outside every
        repo stays free."""
        repo, outside, home = self.repo, self.outside, self.home
        os.symlink(str(repo / "src"), home / "h")
        os.symlink(str(repo / "cache"), home / "c")
        os.symlink(str(repo), self.tmp / "root")
        os.symlink(str(outside), home / "o")
        self.assertEqual(os.path.realpath(home / "h"), str(repo / "src"))
        self.write(outside / "evil.py", "clobbered\n")
        self.write(outside / "data/app.py", "clobbered\n")
        refused = (
            f"cp {outside}/evil.py ~/h/",
            f"cp {outside}/evil.py {home}/h/",
            f"cp {outside}/evil.py {home}/h",
            f"cp {outside}/data/app.py {home}/h/",
            f"cp -R {outside}/data/ {home}/h",
            f"cp -R {outside}/data/ ~/h/",
            f"cp -a {outside}/data {home}/h/",
            f"rsync -a {outside}/data/ {home}/h/",
            f"rsync -t {outside}/evil.py ~/h/",
            f"install {outside}/evil.py {home}/h/",
            f"ln -sf {outside}/evil.py {home}/h/",
            f"cp -t {home}/h {outside}/evil.py",
            f"cp {outside}/evil.py {outside}/notes.md ~/h/",
            f"cp {outside}/evil.py {self.tmp}/root/",
            f"cp -R {outside}/data/ {self.tmp}/root",
            # a link to an ignored directory: a copy into an ignored directory
            f"cp {outside}/evil.py ~/c/",
            f"cp {outside}/evil.py {home}/c",
            f"rsync -a {outside}/data/ {home}/c/",
        )
        for cwd in (outside, repo):
            for command in refused:
                with self.subTest(command=command, cwd=str(cwd)):
                    self.assertRefusedPart(self.bash(command, cwd=cwd), command)
        # beside an allowed write, the copy through the link is the part refused
        command = f"echo x > {outside}/log.txt && cp {outside}/evil.py ~/h/"
        self.assertRefusedPart(
            self.bash(command, cwd=outside), f"cp {outside}/evil.py ~/h/"
        )
        # a link to a directory outside every repo
        self.assertAllowed(
            (
                f"cp {outside}/evil.py ~/o/copy.py",
                f"cp -R {outside}/data/ {home}/o/restored/",
                f"rsync -a {outside}/data/ {home}/o/",
            ),
            cwd=outside,
        )


    # ---- #237: a recursive copy into docs/agent/ through chained symlinks



    def test_ln_no_dereference_onto_an_ignored_symlink_to_an_outside_directory_allowed(self):
        """ln -n and -h treat a destination that is a symlink to a directory as a named
        file, not a directory to link into: `ln -sfn ../.venv .venv` replaces the ignored
        .venv link to ~/.venv, even when ~/.venv/.venv already exists. A real directory
        stays a directory to link into, and a named link out of the exemption stays
        refused. An ignored link to an ignored directory in the repo shows the difference:
        with -n the link itself is replaced (a named destination), without it the new
        link lands inside the ignored directory (refused, ruling b)."""
        repo, home = self.repo, self.home
        os.symlink("../.venv", repo / ".venv")
        self.write(home / ".venv/.venv", "stray\n")
        self.write(home / "other-venv/bin/python", "")
        os.symlink("../src", repo / "cache/srclink")  # ignored name, tracked target
        self.write(repo / "cache/py310/bin/python", "")
        self.write(repo / "cache/py311/bin/python", "")
        os.symlink("../cache/py310", repo / "data/venv")  # ignored link, ignored target
        self.assertEqual(os.path.realpath(repo / ".venv"), str(home / ".venv"))
        self.assertEqual(os.path.realpath(repo / "data/venv"), str(repo / "cache/py310"))
        self.assertAllowed(
            (
                "ln -sfn ../.venv .venv",
                "ln -sfh ../.venv .venv",
                "ln -snf ../.venv .venv",
                "ln -s -f -n ../.venv .venv",
                f"ln -sfn {home}/.venv .venv",
                f"ln -sfn {home}/other-venv .venv",
                "ln -sfn ../other-venv ./.venv",
                # without -n the link lands in ~/.venv, outside every repo, as on main
                "ln -sf ../.venv .venv",
                # -n onto the ignored link to the ignored cache/py310
                "ln -sfn ../cache/py311 data/venv",
                "ln -sfh ../cache/py311 data/venv",
                "ln -nsf ../cache/py311 data/venv",
                f"ln -sfn {repo}/cache/py311 data/venv",
            )
        )
        for command in (
            # without -n or -h: the link lands inside the ignored cache/py310
            "ln -sf ../cache/py311 data/venv",
            "ln -s ../cache/py311 data/venv",
            f"ln -sf {repo}/cache/py311 data/venv",
            "ln -sf ../cache/py311 data/venv/",
        ):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), command)
        self.assertAllowed(
            (
                f"ln -sfn {home}/.venv {repo}/.venv",
                f"cd {repo} && ln -sfn ../.venv .venv",
            ),
            cwd=self.outside,
        )
        for command in (
            # .venv as a real ignored directory: -n links into it (ruling b)
            "ln -sfn ../.venv cache",
            "ln -sfn ../.venv data/",
            # a named link onto an ignored link to tracked src/
            f"ln -sfn {self.outside} cache/srclink",
            f"ln -sfh {self.outside} cache/srclink",
            # -n onto a tracked or unignored name
            "ln -sfn ../.venv venv",
            "ln -sfn ../.venv src",
        ):
            with self.subTest(command=command):
                self.assertRefusedPart(self.bash(command), command)


if __name__ == "__main__":
    unittest.main()
