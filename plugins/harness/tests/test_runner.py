"""Tests for harness-runner (ticket #420): self-hosted CI runners on the Mac for private projects, one
Docker container per repository, and the offline-runner flag in harness-standup and harness-digest.

    python3 -m unittest discover -s plugins/harness/tests -p test_runner.py

Every test has its own HOME with a registry of four projects, each a git repository whose origin is on
github.com (never contacted): alpha, beta and delta are private, gamma is public. The registered name is
the repository name, and `harness-runner add <repo>` takes that name.

`fakebin`, first on PATH and written once for the module, holds:
- `gh`: a wrapper around fakegh.py (HARNESS_GH points at it too). The scenario answers `gh api` on
  `repos/O/R/actions/runners/registration-token` (JSON {"token": ...}, or the bare token when the call
  has `.token` as an argument, as `-q .token` and `--jq .token` do), `repos/O/R/actions/runners` (the
  runner list JSON, `busy` and `status` included) and `repos/O/R` (JSON with `private` and `visibility`),
  each also with a leading slash, and `gh repo view O/R` (JSON with `isPrivate` and `visibility`). The
  paths carry no query string and the answers are JSON (fakegh ignores --jq). Anything else prints `[]`.
- `docker`: FAKE_DOCKER below, a stateful stand-in for the Docker CLI. Its world (containers, named
  volumes, built images) lives in the JSON file FAKEDOCKER_STATE; every call appends {"argv", "env"} to
  FAKEDOCKER_LOG (`env`: the values of its environment). It knows `run`/`create` (the run flags named in
  RUN_VALUE_FLAGS and RUN_BOOL_FLAGS, `-v`/`--volume` and `--mount`; an unknown flag fails as docker
  would), `build` (and `image build`, `buildx build`), `ps`/`container ls`, `inspect`/`container inspect`,
  `image inspect`, `images`/`image ls`, `rm`, `stop`, `kill`, `start`, `restart`, `logs` (which prints
  `Listening for Jobs`), `exec`, `info`, `version`, `volume create|inspect|ls|rm`; `--format` takes Go
  template fields (`{{.State.Status}}`, `{{json .}}`). `run` needs an image that `build` made and records,
  at that moment, the mode and text of its `--env-file`. A container's volume is a bind mount when its
  source is a path, else a named volume, created on first use; a volume in use by a container cannot be
  removed.
- `claude` (answers `agents --json` with an empty fleet), `terminal-notifier` and `osascript` (no-ops), so
  the standup and the digest never reach the real Mac.

Each test's first run of harness-runner is a real process, its further table rows run in this process
(gitrepo_support.run_in_process).

Standup and digest are read for one line per flagged project: a line holding `offline` that names the
project, or that sits under the project's own `## ` heading.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

from gitrepo_support import GIT, run_in_process  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
BIN = os.path.join(PLUGIN, "bin")
TOOL = os.path.join(BIN, "harness-runner")
STANDUP = os.path.join(BIN, "harness-standup")
DIGEST = os.path.join(BIN, "harness-digest")
RUNNER_TEMPLATES = os.path.join(PLUGIN, "templates", "runner")
FAKEGH = os.path.join(HERE, "fakegh.py")
OWNER = "o"
PROJECTS = ("alpha", "beta", "gamma", "delta")
PRIVATE = {"alpha", "beta", "delta"}
# One registration token per repository, so an env file can only hold its own.
TOKENS = {name: f"AQRUNNERTOKEN{name.upper()}7X3Q9Z" for name in PROJECTS}
RUNNER_IDS = {"alpha": 701, "beta": 702, "gamma": 703, "delta": 704}
GIB = 1024 ** 3
MIB = 1024 ** 2
W = {}

FAKE_DOCKER = r'''
import hashlib, json, os, re, stat, sys

STATE = os.environ["FAKEDOCKER_STATE"]
RUN_VALUE_FLAGS = {"--name", "--restart", "-m", "--memory", "--memory-swap", "--memory-reservation", "--cpus",
                   "--env-file", "-v", "--volume", "--mount", "-e", "--env", "-l", "--label", "--network", "--net",
                   "-h", "--hostname", "-u", "--user", "-w", "--workdir", "--entrypoint", "--platform",
                   "--log-driver", "--log-opt", "--pull", "--stop-timeout", "--shm-size", "--cpu-shares",
                   "--cpuset-cpus", "--pids-limit", "--tmpfs", "--security-opt", "--cap-drop", "--ulimit",
                   "--add-host", "--dns", "--label-file", "--stop-signal", "--health-cmd", "--init-path"}
RUN_BOOL_FLAGS = {"-d", "--detach", "--rm", "-i", "--interactive", "-t", "--tty", "-it", "-dit", "-itd", "--init",
                  "--read-only", "-q", "--quiet", "--no-healthcheck"}
BUILD_VALUE_FLAGS = {"-t", "--tag", "-f", "--file", "--build-arg", "--platform", "--progress", "--label", "--target",
                     "--cache-from", "--network", "--iidfile", "--output", "-o", "--builder"}
BUILD_BOOL_FLAGS = {"--pull", "--no-cache", "-q", "--quiet", "--load", "--rm", "--force-rm"}


def load():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {"seq": 0, "containers": {}, "volumes": [], "images": {}, "builds": []}


def save(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, STATE)


def die(message, code=1):
    sys.stderr.write(message + "\n")
    sys.exit(code)


def split_flags(args, value_flags, bool_flags, stop_at_positional=True):
    """[(flag, value)], positionals. `--flag=value` and `-fVALUE` are not split for short flags."""
    flags, rest, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            rest += args[i + 1:]
            break
        if a.startswith("-") and a != "-":
            name, eq, val = a.partition("=")
            if eq and name in value_flags:
                flags.append((name, val))
            elif a in value_flags:
                if i + 1 >= len(args):
                    die(f"flag needs an argument: {a}", 125)
                flags.append((a, args[i + 1]))
                i += 1
            elif a in bool_flags or (eq and name in bool_flags):
                flags.append((name, val if eq else "true"))
            elif not a.startswith("--") and not eq and all("-" + ch in bool_flags for ch in a[1:]):
                flags += [("-" + ch, "true") for ch in a[1:]]          # -aq, -fv
            else:
                die(f"unknown flag: {a}", 125)
        else:
            rest.append(a)
            if stop_at_positional:
                rest += args[i + 1:]
                break
        i += 1
    return flags, rest


def values(flags, *names):
    return [v for f, v in flags if f in names]


def last(flags, *names):
    found = values(flags, *names)
    return found[-1] if found else None


def bytes_of(text):
    if text is None:
        return 0
    m = re.match(r"^(\d+(?:\.\d+)?)([bkmg]?)b?$", text.strip().lower())
    if not m:
        die(f"invalid memory value: {text}", 125)
    return int(float(m.group(1)) * {"": 1, "b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2)])


def mount_of(spec, kind):
    if kind == "mount":
        parts = dict(p.split("=", 1) if "=" in p else (p, "true") for p in spec.split(","))
        source = parts.get("source") or parts.get("src") or ""
        target = parts.get("target") or parts.get("destination") or parts.get("dst") or ""
        mtype = parts.get("type", "volume")
    else:
        pieces = spec.split(":")
        if len(pieces) == 1:
            source, target = "", pieces[0]
        else:
            source, target = pieces[0], pieces[1]
        mtype = "bind" if source.startswith(("/", ".", "~")) else "volume"
    return {"Type": mtype, "Name": source if mtype == "volume" else "", "Source": source, "Destination": target}


def resolve_image(state, ref):
    if ref in state["images"]:
        return state["images"][ref]
    if ":" not in ref.rsplit("/", 1)[-1] and ref + ":latest" in state["images"]:
        return state["images"][ref + ":latest"]
    for build in state["builds"]:
        if build["id"] == ref or build["id"].startswith(ref) or build["id"][7:].startswith(ref):
            return build["id"]
    return None


def view(c):
    """The container as `docker inspect` prints it."""
    return {"Id": c["Id"], "Name": "/" + c["Name"], "Image": c["Image"],
            "State": {"Status": c["State"], "Running": c["State"] == "running",
                      "ExitCode": 0 if c["State"] == "running" else 143},
            "Config": {"Image": c["ImageRef"], "Labels": c["Labels"], "Env": c["Env"], "Cmd": c["Cmd"]},
            "HostConfig": {"Memory": c["Memory"], "NanoCpus": c["NanoCpus"],
                           "RestartPolicy": {"Name": c["Restart"], "MaximumRetryCount": 0},
                           "Binds": [m["Source"] + ":" + m["Destination"] for m in c["Mounts"]
                                     if m["Type"] == "bind"]},
            "Mounts": [dict(m, Source=("/var/lib/docker/volumes/" + m["Name"] + "/_data")
                            if m["Type"] == "volume" else m["Source"]) for m in c["Mounts"]]}


def row(c):
    """The container as `docker ps --format '{{json .}}'` prints it."""
    status = "Up 2 hours" if c["State"] == "running" else "Exited (143) 1 hour ago"
    return {"ID": c["Id"][:12], "Names": c["Name"], "State": c["State"], "Status": status, "Image": c["ImageRef"],
            "Labels": ",".join(f"{k}={v}" for k, v in c["Labels"].items()),
            "Mounts": ",".join(m["Name"] or m["Source"] for m in c["Mounts"]), "Command": " ".join(c["Cmd"])}


def render(template, obj):
    template = template.replace("\\t", "\t").replace("\\n", "\n")
    if template.startswith("table "):
        template = template[6:]

    def field(m):
        value = obj
        for part in [p for p in m.group(2).split(".") if p]:
            value = value.get(part) if isinstance(value, dict) else None
        if m.group(1):
            return json.dumps(value)
        if value is None:
            return "<no value>"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return str(value)

    return re.sub(r"\{\{\s*(json\s+)?\.([\w.]*)\s*\}\}", field, template)


def find(state, ref):
    ref = ref.lstrip("/")
    if ref in state["containers"]:
        return state["containers"][ref]
    for c in state["containers"].values():
        if c["Id"].startswith(ref):
            return c
    return None


def cmd_run(state, args, create=False):
    flags, rest = split_flags(args, RUN_VALUE_FLAGS, RUN_BOOL_FLAGS)
    if not rest:
        die('"docker run" requires at least 1 argument.', 125)
    ref, cmd = rest[0], rest[1:]
    image = resolve_image(state, ref)
    if image is None:
        die(f"Unable to find image '{ref}' locally\nError response from daemon: pull access denied for {ref}", 125)
    name = last(flags, "--name")
    if name and name in state["containers"]:
        die(f'Error response from daemon: Conflict. The container name "/{name}" is already in use', 125)
    env_file = last(flags, "--env-file")
    env_record = None
    if env_file is not None:
        try:
            with open(env_file, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            die(f"open {env_file}: no such file or directory", 125)
        env_record = {"path": os.path.abspath(env_file), "mode": stat.S_IMODE(os.stat(env_file).st_mode),
                      "text": text}
    mounts = [mount_of(v, "volume") for v in values(flags, "-v", "--volume")]
    mounts += [mount_of(v, "mount") for v in values(flags, "--mount")]
    for m in mounts:
        if m["Type"] == "volume" and m["Name"] and m["Name"] not in state["volumes"]:
            state["volumes"].append(m["Name"])
    labels = {}
    for v in values(flags, "-l", "--label"):
        k, _, val = v.partition("=")
        labels[k] = val
    cpus = last(flags, "--cpus")
    state["seq"] += 1
    cid = hashlib.sha256(f"container{state['seq']}".encode()).hexdigest()
    rm = bool(values(flags, "--rm"))
    container = {"Id": cid, "Name": name or f"fake_{state['seq']}", "State": "created" if create else "running",
                 "Image": image, "ImageRef": ref, "Memory": bytes_of(last(flags, "-m", "--memory")),
                 "NanoCpus": int(round(float(cpus) * 1e9)) if cpus else 0,
                 "Restart": last(flags, "--restart") or "no", "Mounts": mounts, "Labels": labels,
                 "Env": values(flags, "-e", "--env"), "EnvFile": env_record, "Cmd": cmd, "Seq": state["seq"]}
    if not (rm and not create):
        state["containers"][container["Name"]] = container
    save(state)
    print(cid)


def cmd_build(state, args):
    flags, rest = split_flags(args, BUILD_VALUE_FLAGS, BUILD_BOOL_FLAGS, stop_at_positional=False)
    if len(rest) != 1:
        die('"docker build" requires exactly 1 argument.', 1)
    context = rest[0]
    dockerfile = last(flags, "-f", "--file") or os.path.join(context, "Dockerfile")
    if not os.path.isabs(dockerfile) and not os.path.exists(dockerfile):
        dockerfile = os.path.join(context, dockerfile)
    if not os.path.isdir(context) or not os.path.isfile(dockerfile):
        die(f"ERROR: failed to read dockerfile: open {dockerfile}: no such file or directory", 1)
    files = sorted(os.path.relpath(os.path.join(d, f), context) for d, _, fs in os.walk(context) for f in fs)
    with open(dockerfile, encoding="utf-8") as fh:
        text = fh.read()
    state["seq"] += 1
    image = "sha256:" + hashlib.sha256(f"image{state['seq']}".encode()).hexdigest()
    tags = [t if ":" in t.rsplit("/", 1)[-1] else t + ":latest" for t in values(flags, "-t", "--tag")]
    for t in tags:
        state["images"][t] = image
    state["builds"].append({"id": image, "tags": tags, "context": os.path.abspath(context), "files": files,
                            "dockerfile": text})
    iidfile = last(flags, "--iidfile")
    if iidfile:
        with open(iidfile, "w", encoding="utf-8") as fh:
            fh.write(image)
    save(state)
    print(image if values(flags, "-q", "--quiet") else f"Successfully built {image[7:19]}")


def filter_ok(c, filters):
    for f in filters:
        key, _, val = f.partition("=")
        if key == "name" and not re.search(val, c["Name"]):
            return False
        if key == "status" and c["State"] != val:
            return False
        if key == "id" and not c["Id"].startswith(val):
            return False
        if key == "label":
            k, eq, v = val.partition("=")
            if k not in c["Labels"] or (eq and c["Labels"][k] != v):
                return False
        if key == "volume" and val not in [m["Name"] or m["Destination"] for m in c["Mounts"]]:
            return False
        if key == "ancestor" and val not in (c["ImageRef"], c["Image"]):
            return False
    return True


def cmd_ps(state, args):
    flags, rest = split_flags(args, {"-f", "--filter", "--format", "-n", "--last"},
                              {"-a", "--all", "-q", "--quiet", "--no-trunc", "-s", "--size", "-l", "--latest"},
                              stop_at_positional=False)
    every = bool(values(flags, "-a", "--all"))
    shown = [c for c in sorted(state["containers"].values(), key=lambda c: -c["Seq"])
             if (every or c["State"] == "running") and filter_ok(c, values(flags, "-f", "--filter"))]
    fmt = last(flags, "--format")
    for c in shown:
        if values(flags, "-q", "--quiet"):
            print(c["Id"] if values(flags, "--no-trunc") else c["Id"][:12])
        elif fmt:
            print(render(fmt, row(c)))
        else:
            print(f"{c['Id'][:12]}   {c['ImageRef']}   {row(c)['Status']}   {c['Name']}")


def cmd_inspect(state, args, kind=None):
    flags, rest = split_flags(args, {"-f", "--format", "--type"}, {"-s", "--size"}, stop_at_positional=False)
    kind = kind or last(flags, "--type")
    out, missing = [], []
    for ref in rest:
        c = find(state, ref) if kind in (None, "container") else None
        if c is not None:
            out.append(view(c))
            continue
        if kind in (None, "image"):
            image = resolve_image(state, ref)
            if image:
                out.append({"Id": image, "RepoTags": [t for t, i in state["images"].items() if i == image]})
                continue
        if kind in (None, "volume") and ref in state["volumes"]:
            out.append({"Name": ref, "Driver": "local", "Mountpoint": f"/var/lib/docker/volumes/{ref}/_data"})
            continue
        missing.append(ref)
    fmt = last(flags, "-f", "--format")
    if fmt:
        for obj in out:
            print(render(fmt, obj))
    else:
        print(json.dumps(out, indent=4))
    if missing:
        what = {"image": "image", "volume": "volume", "container": "container"}.get(kind, "object")
        die("\n".join(f"Error: No such {what}: {m}" for m in missing), 1)


def cmd_images(state, args):
    flags, rest = split_flags(args, {"-f", "--filter", "--format"}, {"-a", "--all", "-q", "--quiet", "--no-trunc"},
                              stop_at_positional=False)
    fmt = last(flags, "--format")
    for tag, image in sorted(state["images"].items()):
        repo, _, version = tag.rpartition(":")
        if rest and rest[0] not in (tag, repo):
            continue
        obj = {"ID": image[7:19], "Repository": repo, "Tag": version}
        if values(flags, "-q", "--quiet"):
            print(image[7:19])
        elif fmt:
            print(render(fmt, obj))
        else:
            print(f"{repo}   {version}   {image[7:19]}")


def cmd_rm(state, args):
    flags, rest = split_flags(args, set(), {"-f", "--force", "-v", "--volumes", "-fv", "-vf"},
                              stop_at_positional=False)
    force = any(f in ("-f", "--force", "-fv", "-vf") for f, _ in flags)
    failed = []
    for ref in rest:
        c = find(state, ref)
        if c is None:
            failed.append(f"Error response from daemon: No such container: {ref}")
        elif c["State"] == "running" and not force:
            failed.append(f"Error response from daemon: You cannot remove a running container {c['Id']}. "
                          "Stop the container before attempting removal or force remove")
        else:
            del state["containers"][c["Name"]]
            print(ref)
    save(state)
    if failed:
        die("\n".join(failed), 1)


def cmd_state(state, args, new_state):
    flags, rest = split_flags(args, {"-t", "--time", "-s", "--signal"}, {"-a", "--attach", "-i"},
                              stop_at_positional=False)
    failed = []
    for ref in rest:
        c = find(state, ref)
        if c is None:
            failed.append(f"Error response from daemon: No such container: {ref}")
        else:
            c["State"] = new_state
            print(ref)
    save(state)
    if failed:
        die("\n".join(failed), 1)


def cmd_volume(state, args):
    if not args:
        die("usage: docker volume COMMAND", 1)
    sub, args = args[0], args[1:]
    if sub == "create":
        flags, rest = split_flags(args, {"-d", "--driver", "--label", "-o", "--opt", "--name"}, set(),
                                  stop_at_positional=False)
        name = (rest or values(flags, "--name"))[0]
        if name not in state["volumes"]:
            state["volumes"].append(name)
        save(state)
        print(name)
    elif sub == "inspect":
        cmd_inspect(state, args, kind="volume")
    elif sub in ("ls", "list"):
        flags, rest = split_flags(args, {"-f", "--filter", "--format"}, {"-q", "--quiet"},
                                  stop_at_positional=False)
        fmt = last(flags, "--format")
        for name in sorted(state["volumes"]):
            if any(f.startswith("name=") and not re.search(f[5:], name) for f in values(flags, "-f", "--filter")):
                continue
            if fmt:
                print(render(fmt, {"Name": name, "Driver": "local"}))
            elif values(flags, "-q", "--quiet"):
                print(name)
            else:
                print(f"local     {name}")
    elif sub in ("rm", "remove"):
        flags, rest = split_flags(args, set(), {"-f", "--force"}, stop_at_positional=False)
        failed = []
        for name in rest:
            users = [c["Name"] for c in state["containers"].values() if name in [m["Name"] for m in c["Mounts"]]]
            if users:
                failed.append(f"Error response from daemon: remove {name}: volume is in use - [{users[0]}]")
            elif name in state["volumes"]:
                state["volumes"].remove(name)
                print(name)
            elif not values(flags, "-f", "--force"):
                failed.append(f"Error response from daemon: get {name}: no such volume")
        save(state)
        if failed:
            die("\n".join(failed), 1)
    else:
        die(f"fakedocker: unsupported: docker volume {sub}", 125)


def main():
    argv = sys.argv[1:]
    log = os.environ.get("FAKEDOCKER_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"argv": argv, "env": sorted(set(os.environ.values()))}) + "\n")
    if not argv:
        die("usage: docker COMMAND", 1)
    state = load()
    cmd, args = argv[0], argv[1:]
    if cmd == "container" and args:
        cmd, args = {"ls": "ps", "list": "ps"}.get(args[0], args[0]), args[1:]
        if cmd == "inspect":
            return cmd_inspect(state, args, kind="container")
    elif cmd == "image" and args:
        cmd, args = {"ls": "images", "list": "images"}.get(args[0], args[0]), args[1:]
        if cmd == "inspect":
            return cmd_inspect(state, args, kind="image")
    elif cmd == "buildx" and args and args[0] == "build":
        cmd, args = "build", args[1:]
    if cmd == "run":
        return cmd_run(state, args)
    if cmd == "create":
        return cmd_run(state, args, create=True)
    if cmd == "build":
        return cmd_build(state, args)
    if cmd == "ps":
        return cmd_ps(state, args)
    if cmd == "inspect":
        return cmd_inspect(state, args)
    if cmd == "images":
        return cmd_images(state, args)
    if cmd == "rm":
        return cmd_rm(state, args)
    if cmd in ("stop", "kill"):
        return cmd_state(state, args, "exited")
    if cmd in ("start", "restart"):
        return cmd_state(state, args, "running")
    if cmd == "logs":
        ref = [a for a in args if not a.startswith("-") and not a.isdigit()]
        if not ref or find(state, ref[-1]) is None:
            die(f"Error response from daemon: No such container: {ref[-1] if ref else ''}", 1)
        print("Current runner version: '2.338.2'")
        print("2026-11-23 08:00:00Z: Listening for Jobs")
        return None
    if cmd == "exec":
        return None
    if cmd in ("info", "version"):
        fmt = [a for a in args if "{{" in a]
        print("27.3.1" if fmt else "Server Version: 27.3.1")
        return None
    if cmd == "volume":
        return cmd_volume(state, args)
    die("fakedocker: unsupported: docker " + " ".join(argv), 125)


main()
'''


# ------------------------------------------------------------------ module fixtures


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def executable(path, text):
    write(path, text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def base_env(home):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HARNESS_", "FAKEGH_", "FAKEDOCKER_", "GIT_", "CLAUDE_"))}
    folders = [W["fakebin"]] + ([os.path.dirname(GIT)] if os.path.dirname(GIT) not in ("/usr/bin", "/bin")
                                else []) + ["/usr/bin", "/bin"]
    env.update(HOME=home, PATH=os.pathsep.join(folders), HARNESS_GH=os.path.join(W["fakebin"], "gh"),
               GIT_CONFIG_NOSYSTEM="1", GIT_CEILING_DIRECTORIES=W["root"],
               GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@localhost",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@localhost",
               GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="maintenance.auto", GIT_CONFIG_VALUE_0="false")
    return env


def setUpModule():
    root = os.path.realpath(tempfile.mkdtemp(prefix="harness-runner-"))
    W.update(root=root, fakebin=os.path.join(root, "fakebin"))
    try:
        py = sys.executable
        docker = write(os.path.join(root, "fakedocker.py"), FAKE_DOCKER)
        executable(os.path.join(W["fakebin"], "docker"), f'#!/bin/sh\nexec "{py}" "{docker}" "$@"\n')
        executable(os.path.join(W["fakebin"], "gh"), f'#!/bin/sh\nexec "{py}" "{FAKEGH}" "$@"\n')
        executable(os.path.join(W["fakebin"], "claude"),
                   '#!/bin/sh\n[ "$1 $2" = "agents --json" ] || exit 64\necho "[]"\n')
        for name in ("terminal-notifier", "osascript"):
            executable(os.path.join(W["fakebin"], name), "#!/bin/sh\nexit 0\n")
        env = base_env(os.path.join(root, "githome"))
        for name in PROJECTS:
            path = os.path.join(root, name)
            os.makedirs(path)
            write(os.path.join(path, "README.md"), f"# {name}\n")
            for args in (("init", "-q", "-b", "main"), ("add", "-A"),
                         ("-c", "commit.gpgsign=false", "commit", "-q", "-m", "Initial commit"),
                         ("remote", "add", "origin", f"https://github.com/{OWNER}/{name}.git")):
                subprocess.run([GIT, "-C", path, *args], env=env, check=True, capture_output=True)
            W[name] = path
    except BaseException:
        shutil.rmtree(root, True)
        raise


def tearDownModule():
    shutil.rmtree(W.get("root", ""), True)


def runner(name, status="online", busy=False):
    """A runner as GitHub's REST API lists it."""
    return {"id": RUNNER_IDS[name], "name": f"harness-mac-{name}", "os": "Linux", "status": status, "busy": busy,
            "labels": [{"id": 1, "name": "self-hosted", "type": "read-only"},
                       {"id": 2, "name": "Linux", "type": "read-only"},
                       {"id": 3, "name": "ARM64", "type": "read-only"},
                       {"id": 4, "name": "harness-mac", "type": "custom"}]}


def scenario(runners):
    """fakegh calls for every project: `runners` maps a project to its GitHub runner list (missing: none)."""
    calls = []
    for name in PROJECTS:
        slug, private = f"{OWNER}/{name}", name in PRIVATE
        listed = runners.get(name, [])
        for pre in ("", "/"):
            calls += [
                {"contains": [f"{pre}repos/{slug}/actions/runners/registration-token", ".token"],
                 "stdout": TOKENS[name] + "\n"},
                {"contains": [f"{pre}repos/{slug}/actions/runners/registration-token"],
                 "stdout_json": {"token": TOKENS[name], "expires_at": "2026-11-23T09:00:00Z"}},
                {"contains": [f"{pre}repos/{slug}/actions/runners"],
                 "stdout_json": {"total_count": len(listed), "runners": listed}},
                {"contains": [f"{pre}repos/{slug}"],
                 "stdout_json": {"full_name": slug, "name": name, "private": private,
                                 "visibility": "private" if private else "public"}},
            ]
        calls.append({"contains": ["repo", "view", slug],
                      "stdout_json": {"nameWithOwner": slug, "name": name, "isPrivate": private,
                                      "visibility": "PRIVATE" if private else "PUBLIC"}})
    return {"calls": calls, "default": {"stdout": "[]"}}


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="test-", dir=W["root"]))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".claude", "harness")
        write(os.path.join(self.state, "projects.json"), json.dumps({"version": 1, "projects": [
            {"path": W[n], "name": n, "billable": False, "client": "", "notes": ""} for n in PROJECTS]}))
        self.ghscenario = os.path.join(self.tmp, "gh-scenario.json")
        self.ghlog = os.path.join(self.tmp, "gh.log")
        self.dstate = os.path.join(self.tmp, "docker-state.json")
        self.dlog = os.path.join(self.tmp, "docker.log")
        self.github({"alpha": [runner("alpha")], "beta": [runner("beta")], "gamma": [runner("gamma", "offline")]})
        self.ran_a_process = False

    # ------------------------------------------------------------ helpers
    def github(self, runners):
        write(self.ghscenario, json.dumps(scenario(runners)))

    def env(self):
        env = base_env(self.home)
        env.update(FAKEGH_SCENARIO=self.ghscenario, FAKEGH_LOG=self.ghlog, FAKEDOCKER_STATE=self.dstate,
                   FAKEDOCKER_LOG=self.dlog)
        return env

    def run_tool(self, tool, *args):
        """A tool in self.tmp: a real process the first time in a test, then in this process."""
        if self.ran_a_process:
            return run_in_process(tool, args, cwd=self.tmp, env=self.env())
        self.ran_a_process = True
        return subprocess.run([sys.executable, tool, *args], cwd=self.tmp, env=self.env(), capture_output=True,
                              text=True, timeout=60)

    def runner_ok(self, *args):
        p = self.run_tool(TOOL, *args)
        self.assertEqual(p.returncode, 0, f"harness-runner {' '.join(args)}: {p.stdout}{p.stderr}")
        self.assertNotIn("Traceback", p.stderr)
        return p

    def docker(self):
        try:
            with open(self.dstate, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return {"seq": 0, "containers": {}, "volumes": [], "images": {}, "builds": []}

    def set_docker(self, state):
        write(self.dstate, json.dumps(state))

    def jsonl(self, path):
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def gh_calls(self, since=0):
        return [c["argv"] for c in self.jsonl(self.ghlog)][since:]

    def token_requests(self, calls, name=None):
        names = [name] if name else PROJECTS
        paths = {f"repos/{OWNER}/{n}/actions/runners/registration-token" for n in names}
        return [c for c in calls if any(a.lstrip("/") in paths for a in c)]

    def deletes(self, calls):
        """The runner ids a DELETE call removed on GitHub."""
        out = []
        for c in calls:
            if any("DELETE" in a.upper() for a in c):
                for a in c:
                    m = re.match(r"^/?repos/[^/]+/[^/]+/actions/runners/(\d+)$", a)
                    if m:
                        out.append(int(m.group(1)))
        return out

    def container(self, name):
        return self.docker()["containers"].get(f"runner-{name}")

    def volumes_of(self, c):
        return [(m["Name"], m["Destination"]) for m in c["Mounts"] if m["Type"] == "volume"]

    def seeded(self, states):
        """A docker world with the runner image built and a container per entry of `states`
        ({name: 'running'|'exited'}), each with its own named volume."""
        image = "sha256:" + hashlib.sha256(b"image1").hexdigest()
        state = {"seq": 10, "volumes": [], "images": {"harness-runner:2.338.2": image}, "containers": {},
                 "builds": [{"id": image, "tags": ["harness-runner:2.338.2"], "context": RUNNER_TEMPLATES,
                             "files": ["Dockerfile", "entrypoint.sh"], "dockerfile": ""}]}
        for i, (name, st) in enumerate(states.items()):
            vol = f"runner-{name}"
            state["volumes"].append(vol)
            state["containers"][f"runner-{name}"] = {
                "Id": hashlib.sha256(f"container{i + 2}".encode()).hexdigest(), "Name": f"runner-{name}", "State": st, "Image": image,
                "ImageRef": "harness-runner:2.338.2", "Memory": GIB, "NanoCpus": 2 * 10 ** 9,
                "Restart": "unless-stopped", "Labels": {}, "Env": [], "EnvFile": None, "Cmd": [], "Seq": i + 1,
                "Mounts": [{"Type": "volume", "Name": vol, "Source": vol, "Destination": "/home/runner/config"}]}
        self.set_docker(state)

    def flagged(self, text):
        """The projects a standup or digest flags: a line holding `offline` that names the project, or that
        sits under the project's own `## ` heading."""
        out, heading = set(), ""
        for line in text.splitlines():
            if line.startswith("## "):
                heading = line
                continue
            if "offline" not in line.lower():
                continue
            for name in PROJECTS:
                pattern = rf"\b{name}\b"
                if re.search(pattern, line) or re.search(pattern, heading):
                    out.add(name)
        return out


# =========================================================================== add
class Add(RunnerTest):
    def test_add_registers_through_a_private_env_file_and_starts_the_container(self):
        """Definition of done 1 (add): each private repository gets runner-<repo> with --restart unless-stopped,
        its own memory and CPU caps and its own named volume, from an image built from templates/runner; the
        registration token reaches the container only through a mode-600 env file that is gone afterwards and
        is on no command line, in no environment and in no output."""
        rows = [  # (repo, extra args, memory in bytes, NanoCpus or None when not given)
            ("alpha", ["--memory", "3g", "--cpus", "6"], 3 * GIB, 6 * 10 ** 9),
            ("beta", ["--memory", "1g"], GIB, None),
            ("delta", ["--memory", "2048m", "--cpus", "1.5"], 2048 * MIB, 1500 * 10 ** 6),
        ]
        outputs = []
        for name, extra, memory, cpus in rows:
            with self.subTest(repo=name):
                p = self.runner_ok("add", name, *extra)
                outputs.append(p.stdout + p.stderr)
                c = self.container(name)
                self.assertIsNotNone(c, f"no container runner-{name}: {sorted(self.docker()['containers'])}")
                self.assertEqual(c["State"], "running")
                self.assertEqual(c["Restart"], "unless-stopped")
                self.assertEqual(c["Memory"], memory)
                if cpus is not None:
                    self.assertEqual(c["NanoCpus"], cpus)
                self.assertEqual([m for m in c["Mounts"] if m["Type"] != "volume"], [], "no host mounts")
                self.assertEqual(len(self.volumes_of(c)), 1, c["Mounts"])
                self.assertTrue(self.volumes_of(c)[0][0], "a named volume, not an anonymous one")
                self.assertIn(c["Image"], [b["id"] for b in self.docker()["builds"]], "an image add built")
                env_file = c["EnvFile"]
                self.assertIsNotNone(env_file, "the token goes through --env-file")
                self.assertEqual(env_file["mode"], 0o600)
                self.assertIn(TOKENS[name], env_file["text"])
                self.assertIn(f"{OWNER}/{name}", env_file["text"] + " ".join(c["Env"]))
                for other in PROJECTS:
                    if other != name:
                        self.assertNotIn(TOKENS[other], env_file["text"])
                self.assertFalse(os.path.exists(env_file["path"]), "the env file is removed afterwards")
                self.assertEqual(len(self.token_requests(self.gh_calls(), name)), 1)
                request = self.token_requests(self.gh_calls(), name)[0]
                self.assertTrue(any("POST" in a.upper() for a in request), request)

        volumes = [self.volumes_of(self.container(n))[0][0] for n, *_ in rows]
        self.assertEqual(len(set(volumes)), len(rows), f"one volume per repository: {volumes}")
        build = self.docker()["builds"][-1]
        with open(os.path.join(RUNNER_TEMPLATES, "Dockerfile"), encoding="utf-8") as fh:
            self.assertEqual(build["dockerfile"], fh.read(), "the image is built from templates/runner")
        self.assertTrue({"Dockerfile", "entrypoint.sh"} <= set(build["files"]), build["files"])

        command_lines = [c["argv"] for c in self.jsonl(self.dlog)] + self.gh_calls()
        environments = [c["env"] for c in self.jsonl(self.dlog)]
        for name in PROJECTS:
            token = TOKENS[name]
            self.assertFalse([a for a in command_lines if any(token in x for x in a)], "token on a command line")
            self.assertFalse([e for e in environments if any(token in v for v in e)], "token in docker's env")
            self.assertFalse([o for o in outputs if token in o], "token printed")

    def test_add_refuses_a_public_repository(self):
        """Definition of done 1 (add): a public repository never gets a self-hosted runner (fork PRs would run
        on the Mac): no token requested, no container, a nonzero exit with one line saying it is public; the
        private repository beside it is added."""
        self.runner_ok("add", "alpha", "--memory", "1g")
        p = self.run_tool(TOOL, "add", "gamma", "--memory", "1g")
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
        self.assertIn("public", p.stderr.lower())
        self.assertIsNone(self.container("gamma"))
        self.assertEqual(sorted(self.docker()["containers"]), ["runner-alpha"])
        self.assertEqual(self.token_requests(self.gh_calls(), "gamma"), [])


# =========================================================================== registration survives
class KeepsRegistration(RunnerTest):
    def test_a_recreated_container_mounts_the_same_volume(self):
        """Definition of done 2: runner config lives in a named volume per repository, so a container removed
        by hand (Docker Desktop reset, docker rm) and added again mounts the volume it had."""
        self.runner_ok("add", "alpha", "--memory", "3g", "--cpus", "6")
        self.runner_ok("add", "beta", "--memory", "1g")
        before = {n: self.volumes_of(self.container(n)) for n in ("alpha", "beta")}
        state = self.docker()
        del state["containers"]["runner-alpha"]                       # docker rm -f runner-alpha, by hand
        self.set_docker(state)

        self.runner_ok("add", "alpha", "--memory", "3g", "--cpus", "6")

        self.assertEqual(self.volumes_of(self.container("alpha")), before["alpha"])
        self.assertEqual(self.volumes_of(self.container("beta")), before["beta"])
        self.assertNotEqual(before["alpha"], before["beta"])

    def test_upgrade_rebuilds_and_recreates_only_idle_runners_keeping_their_registration(self):
        """Definitions of done 1 (upgrade) and 2: upgrade builds the image again and re-creates the idle
        runner's container on it with the same volume, caps and restart policy, asking GitHub for no new token
        and deleting no registration; the busy runner's container is left as it was."""
        rows = [  # (alpha busy, beta busy): the idle ones are re-created
            (False, True),
            (True, False),
        ]
        self.runner_ok("add", "alpha", "--memory", "3g", "--cpus", "6")
        self.runner_ok("add", "beta", "--memory", "1g", "--cpus", "2")
        for alpha_busy, beta_busy in rows:
            with self.subTest(alpha_busy=alpha_busy, beta_busy=beta_busy):
                self.github({"alpha": [runner("alpha", busy=alpha_busy)], "beta": [runner("beta", busy=beta_busy)],
                             "gamma": [runner("gamma", "offline")]})
                before = {n: self.container(n) for n in ("alpha", "beta")}
                builds = len(self.docker()["builds"])
                calls = len(self.gh_calls())

                self.runner_ok("upgrade")

                state = self.docker()
                self.assertEqual(len(state["builds"]), builds + 1, "upgrade builds the image again")
                newest = state["builds"][-1]["id"]
                for name, busy in (("alpha", alpha_busy), ("beta", beta_busy)):
                    old, new = before[name], self.container(name)
                    self.assertIsNotNone(new, f"runner-{name} is gone after upgrade")
                    if busy:
                        self.assertEqual((new["Id"], new["Image"]), (old["Id"], old["Image"]), f"busy {name} kept")
                    else:
                        self.assertNotEqual(new["Id"], old["Id"], f"idle {name} re-created")
                        self.assertEqual(new["Image"], newest)
                        self.assertEqual(new["State"], "running")
                    self.assertEqual(self.volumes_of(new), self.volumes_of(old))
                    self.assertEqual((new["Memory"], new["NanoCpus"], new["Restart"]),
                                     (old["Memory"], old["NanoCpus"], old["Restart"]))
                upgrade_calls = self.gh_calls(calls)
                self.assertEqual(self.token_requests(upgrade_calls), [], "a re-created runner needs no new token")
                self.assertEqual(self.deletes(upgrade_calls), [])


# =========================================================================== remove
class Remove(RunnerTest):
    def test_remove_deletes_the_container_its_volume_and_the_github_registration(self):
        """Definition of done 1 (remove): the container, its volume (its config names a registration that no
        longer exists) and the runner on GitHub go; the other repository's runner stays."""
        self.runner_ok("add", "alpha", "--memory", "3g")
        self.runner_ok("add", "beta", "--memory", "1g")
        volumes = {n: self.volumes_of(self.container(n))[0][0] for n in ("alpha", "beta")}
        calls = len(self.gh_calls())

        self.runner_ok("remove", "alpha")

        state = self.docker()
        self.assertIsNone(self.container("alpha"))
        self.assertNotIn(volumes["alpha"], state["volumes"])
        self.assertIsNotNone(self.container("beta"))
        self.assertIn(volumes["beta"], state["volumes"])
        self.assertEqual(self.deletes(self.gh_calls(calls)), [RUNNER_IDS["alpha"]])


# =========================================================================== list
class List(RunnerTest):
    def test_list_shows_container_and_github_state_of_every_private_project(self):
        """Definition of done 1 (list): one entry per private registered project, in registry order, with its
        container's state and GitHub's online/offline; the public project is never listed, even with a runner
        registered on GitHub."""
        rows = [  # ({project: container state}, {project: GitHub status}, expected {project: (container, github)})
            ({"alpha": "running", "beta": "exited"}, {"alpha": "online", "beta": "offline"},
             {"alpha": ("running", "online"), "beta": ("exited", "offline"), "delta": ("missing", "missing")}),
            ({"alpha": "exited", "beta": "running", "delta": "running"},
             {"alpha": "offline", "beta": "online", "delta": "online"},
             {"alpha": ("exited", "offline"), "beta": ("running", "online"), "delta": ("running", "online")}),
        ]
        for containers, github, expected in rows:
            with self.subTest(containers=containers, github=github):
                self.seeded(containers)
                runners = {n: [runner(n, status)] for n, status in github.items()}
                runners["gamma"] = [runner("gamma", "offline")]
                self.github(runners)

                p = self.runner_ok("list", "--json")
                entries = json.loads(p.stdout)
                self.assertEqual([e["name"] for e in entries], ["alpha", "beta", "delta"])
                self.assertEqual({e["name"]: (e["container"], e["github"]) for e in entries}, expected)
                self.assertEqual({e["name"]: e["repo"] for e in entries},
                                 {n: f"{OWNER}/{n}" for n in ("alpha", "beta", "delta")})

                p = self.runner_ok("list")
                for name in ("alpha", "beta", "delta"):
                    self.assertRegex(p.stdout, rf"\b{name}\b")
                self.assertNotRegex(p.stdout, r"\bgamma\b")
                offline = {n for n, (_, gh) in expected.items() if gh == "offline"}
                lines = [ln for ln in p.stdout.splitlines() if "offline" in ln.lower()]
                self.assertEqual({n for n in PROJECTS for ln in lines if re.search(rf"\b{n}\b", ln)}, offline)


# =========================================================================== standup and digest
class Flags(RunnerTest):
    def test_standup_and_digest_flag_an_offline_runner_of_a_private_project(self):
        """Definition of done 3: a private project whose runner GitHub reports offline is flagged in the standup
        and the digest (its PRs queue instead of failing); one whose runner is online is not, nor a public
        project, whatever GitHub says of a runner there."""
        rows = [  # ({project: (container state, GitHub status)}, the private projects flagged)
            ({"alpha": ("running", "online"), "beta": ("exited", "offline"), "delta": ("running", "online")},
             {"beta"}),
            ({"alpha": ("exited", "offline"), "beta": ("running", "online"), "delta": ("running", "online")},
             {"alpha"}),
        ]
        for world, expected in rows:
            with self.subTest(world=world):
                self.seeded({n: c for n, (c, _) in world.items()})
                runners = {n: [runner(n, g)] for n, (_, g) in world.items()}
                runners["gamma"] = [runner("gamma", "offline")]
                self.github(runners)
                for tool in (STANDUP, DIGEST):
                    p = self.run_tool(tool)
                    self.assertEqual(p.returncode, 0, f"{os.path.basename(tool)}: {p.stdout}{p.stderr}"[-3000:])
                    self.assertEqual(self.flagged(p.stdout), expected, f"{os.path.basename(tool)}:\n{p.stdout}")


# =========================================================================== templates
class Templates(unittest.TestCase):
    def test_runner_templates_exist_with_the_runner_version_pinned(self):
        """Outline 1: the Dockerfile and entrypoint live in templates/runner, the runner version is a fixed
        x.y.z (never `latest`), and the harness-mac label the scaffold's runs-on names is given to the runner."""
        dockerfile = os.path.join(RUNNER_TEMPLATES, "Dockerfile")
        entrypoint = os.path.join(RUNNER_TEMPLATES, "entrypoint.sh")
        for path in (dockerfile, entrypoint):
            self.assertTrue(os.path.isfile(path), path)
        with open(dockerfile, encoding="utf-8") as fh:
            text = fh.read()
        from_lines = [ln for ln in text.splitlines() if ln.strip().upper().startswith(("FROM ", "ARG "))]
        self.assertTrue(any(re.search(r"\b\d+\.\d+\.\d+\b", ln) for ln in from_lines), from_lines)
        self.assertNotRegex(text, r":latest\b")
        texts = ""
        for name in os.listdir(RUNNER_TEMPLATES):
            with open(os.path.join(RUNNER_TEMPLATES, name), encoding="utf-8") as fh:
                texts += fh.read()
        self.assertIn("harness-mac", texts)
        self.assertEqual(subprocess.run(["bash", "-n", entrypoint]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
