"""Settings and globs every harness tool and hook reads: the `env` object of a project's
.claude settings files (or of the settings.json committed at a ref), one setting from the
environment or that object (or from the object alone when the session is another project's),
a comma-separated pattern list, and the repo-relative glob matcher behind HARNESS_TEST_PATTERNS
and HARNESS_SOURCE_EXEMPT. See docs/design.md (harness_lib).

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess

from harness_lib.paths import canonical_path
from harness_lib.repo import main_checkout

GIT_TIMEOUT = 10  # seconds committed_env's git show may take
# The defaults of HARNESS_TEST_PATTERNS and HARNESS_SOURCE_EXEMPT when a project sets none: the one
# definition that tests_first, harness-red, the auto-land gate and harness-work cost all read.
DEFAULT_TEST_PATTERNS = ("tests/**,test/**,**/test_*.py,**/*_test.py,**/conftest.py,**/*_test.js,**/*.test.js,"
                         "**/*.test.ts,**/*_test.ts,spec/**")
DEFAULT_SOURCE_EXEMPT = ("docs/**,**/*.md,.claude/**,.github/**,.githooks/**,**/*.toml,**/*.cfg,**/*.ini,**/*.json,"
                         "**/*.yaml,**/*.yml,**/*.txt,**/*.lock,.gitignore,.worktreeinclude,LICENSE*")
# Configuration that decides which tests run and how, matched with glob_match (an entry without / matches
# the basename): tests for tests_first's role rules, and never skipped by verify_on_stop.
TEST_CONFIG = (".claude/settings.json", ".claude/settings.local.json", "pytest.ini", "pyproject.toml", "setup.cfg",
               "tox.ini", "conftest.py", "jest.config.*", "vitest.config.*", "package.json")


def settings_env(root, names=("settings.json", "settings.local.json")):
    """`env` from `root`/.claude/<name> for each of `names` in order, a later file winning
    key by key (by default settings.local.json over settings.json). A missing, unreadable
    or broken file, or one whose `env` is not an object, counts as no file."""
    merged = {}
    for name in names:
        try:
            with open(os.path.join(root, ".claude", name), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        env = data.get("env") if isinstance(data, dict) else None
        if isinstance(env, dict):
            merged.update(env)
    return merged


def committed_env(main, ref="origin/main"):
    """`env` from the .claude/settings.json committed at `ref` in the repository at `main`
    (`git show`), so neither the working tree nor settings.local.json counts. A missing file
    or ref, a failing git, a file that is not JSON or not an object, or an `env` that is not
    an object gives {}."""
    try:
        out = subprocess.run(["git", "-C", main, "show", f"{ref}:.claude/settings.json"], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT)
        data = json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    env = data.get("env") if isinstance(data, dict) else None
    return env if isinstance(env, dict) else {}


def setting(name, file_env):
    """The environment first, then the settings files' `env` (a string or number, never a
    bool); stripped, and None when unset or blank."""
    value = os.environ.get(name)
    if value is not None and value.strip():
        return value.strip()
    return setting_from(name, file_env)


def setting_from(name, file_env):
    """The settings files' `env` alone (a string or number, never a bool); stripped, and None
    when unset or blank."""
    value = file_env.get(name)
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def other_project(main):
    """Is the session another project's? True when CLAUDE_PROJECT_DIR names a folder whose main
    checkout is not `main` (a canonical main checkout, as repo_facts gives it): another
    repository, a folder outside any repository, or one git cannot read. Unset or blank, the
    main checkout itself (any spelling), a worktree of it or a folder inside one: False."""
    project = (os.environ.get("CLAUDE_PROJECT_DIR") or "").strip()
    if not project or canonical_path(project) == main:
        return False
    return main_checkout(project) != main


def project_setting(name, file_env, foreign):
    """setting(), except that in another project's session (`foreign`, from other_project) the
    environment carries that project's settings, so only this repository's `env` counts."""
    return setting_from(name, file_env) if foreign else setting(name, file_env)


def patterns(text):
    """A comma-separated list, each entry stripped, empty entries dropped."""
    return [p.strip() for p in text.split(",") if p.strip()]


def glob_match(pattern, path):
    """Does the repo-relative `path` (/ separators) match `pattern`? Split on /, `**` takes
    zero or more segments, other segments use fnmatchcase; a pattern without / matches the
    basename. Surrounding whitespace in the pattern does not count; an empty pattern
    matches nothing."""
    pattern = pattern.strip()
    if not pattern:
        return False
    if "/" not in pattern:
        return fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern)
    return _segments_match(pattern.split("/"), path.split("/"))


def _segments_match(pats, parts):
    if not pats:
        return not parts
    if pats[0] == "**":
        return any(_segments_match(pats[1:], parts[i:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], pats[0]) and _segments_match(pats[1:], parts[1:])
