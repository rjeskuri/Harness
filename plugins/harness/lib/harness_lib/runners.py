"""The self-hosted runners on the Mac (harness-runner, #420): one Docker container `runner-<repo>` per private
GitHub repository, registered as `harness-mac-<repo>` with the label `harness-mac`. harness-runner builds on
these helpers; harness-standup and harness-digest call runner_checks() to flag a runner that is not online.

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import concurrent.futures
import json
import shutil
import subprocess

from harness_lib.cli import Problem
from harness_lib.gh import gh_json, repo_slug
from harness_lib.text import first_line

CONTAINER_PREFIX = "runner-"
RUNNER_PREFIX = "harness-mac-"
CONFIG_DIR = "/home/runner/config"     # where the repository's named volume holds the registration
DOCKER_TIMEOUT = 5                     # seconds for a docker query (ps, inspect, logs)
CHECK_BUDGET = 6                       # seconds for one project's GitHub runner list in runner_checks


def docker_binary():
    """docker on PATH, or None."""
    return shutil.which("docker")


def container_name(repo):
    return CONTAINER_PREFIX + repo


def runner_name(repo):
    return RUNNER_PREFIX + repo


def github_slug(origin_url):
    """OWNER/REPO for an origin on github.com, else None."""
    slug = repo_slug(origin_url or "")
    return slug if slug and slug.count("/") == 1 else None


def origin_slug(path, timeout=DOCKER_TIMEOUT):
    """The github.com OWNER/REPO of the project at `path` (its origin remote), else None."""
    try:
        done = subprocess.run(["git", "--no-optional-locks", "-C", path, "remote", "get-url", "origin"],
                              capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return github_slug(done.stdout.strip()) if done.returncode == 0 else None


def docker_containers(docker, timeout=DOCKER_TIMEOUT):
    """{container name: state} of every runner-* container, stopped ones included; Problem when docker does not
    answer (Docker Desktop not running)."""
    try:
        done = subprocess.run([docker, "ps", "-a", "--filter", f"name={CONTAINER_PREFIX}", "--format", "{{json .}}"],
                              capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Problem(f"docker ps timed out after {timeout} s") from None
    except OSError as err:
        raise Problem(f"cannot run docker ({docker}): {err.strerror or err}") from None
    if done.returncode != 0:
        raise Problem(f"docker ps failed: {first_line(done.stderr) or f'exit {done.returncode}'}")
    out = {}
    for line in done.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        name = str(row.get("Names") or "") if isinstance(row, dict) else ""
        if name.startswith(CONTAINER_PREFIX):
            out[name] = str(row.get("State") or "unknown")
    return out


def github_runner(slug, cwd=None, timeout=60):
    """GitHub's entry for the repository's runner harness-mac-<repo> (id, status, busy, ...), or None when none
    is registered; Problem when the list cannot be read."""
    data = gh_json(["api", f"repos/{slug}/actions/runners"], cwd, timeout=timeout)
    runners = data.get("runners") if isinstance(data, dict) else None
    want = runner_name(slug.split("/")[-1])
    for r in runners if isinstance(runners, list) else []:
        if isinstance(r, dict) and r.get("name") == want:
            return r
    return None


def runner_checks(paths, budget=CHECK_BUDGET):
    """({path: {"container", "state", "github", "error"}}, docker_error) for the projects at `paths` whose GitHub
    repository has a runner-<repo> container. `github` is GitHub's status (online, offline) or "missing" (no
    runner of that name registered), None with `error` when it could not be read. ({}, None) when docker is not
    on PATH or there is no runner container, so no gh call is made; ({}, why) when docker does not answer."""
    docker = docker_binary()
    if docker is None or not paths:
        return {}, None
    try:
        containers = docker_containers(docker)
    except Problem as err:
        return {}, str(err)
    if not containers:
        return {}, None

    def one(path):
        slug = origin_slug(path)
        name = container_name(slug.split("/")[1]) if slug else None
        if name not in containers:
            return None
        info = {"container": name, "state": containers[name], "github": None, "error": None}
        try:
            found = github_runner(slug, path, timeout=budget)
            info["github"] = str(found.get("status") or "unknown") if found else "missing"
        except Problem as err:
            info["error"] = str(err)
        except Exception as err:          # one project never stops the others
            info["error"] = f"{type(err).__name__}: {' '.join(str(err).split())}"
        return info

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, len(paths)))) as pool:
        found = list(pool.map(one, paths))
    return {p: info for p, info in zip(paths, found) if info}, None


def runner_problem(info):
    """The one-line reason a checked runner needs attention, or None when GitHub has it online."""
    if not info:
        return None
    if info.get("error"):
        return f"{info['container']}: could not read GitHub's runners ({info['error']})"
    if info.get("github") == "online":
        return None
    where = "not registered on GitHub" if info.get("github") == "missing" else f"{info.get('github')} on GitHub"
    return (f"{info['container']} is {where} (container {info.get('state')}): its pull requests' CI queues, "
            f"for up to 24 h, instead of failing; `harness-runner list`")


DOCKER_DOWN = "self-hosted runners are offline and private CI queues: start Docker Desktop ({why})"
