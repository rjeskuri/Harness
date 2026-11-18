"""A template git repository built once per test module, its per-test copies, the git binary itself,
and a script run in the test's own process.

Not a test module (no `test_` prefix). A plain `shutil.copytree` of a fresh repository once
failed a CI run (#365): the template's commit had started git's detached background
maintenance, which removed `.git/objects/maintenance.lock` mid-copy. So `make_template` turns
auto gc and auto maintenance off in the repository's own config before its commit (every copy
carries that config), and `copy_repo` skips git's transient `*.lock` files inside `.git`
instead of copying a stale lock or failing on a vanished one. #318 should reuse these.

#378 (epic #374): `GIT` is the git binary itself (on macOS not Apple's /usr/bin/git, an xcrun
shim that costs about 14 ms more a call), and `run_in_process` runs a tool or hook script in this
process under the contract its own process would keep, so a table row pays for neither a Python
start nor a compile of the script.
"""

from __future__ import annotations

import atexit
import builtins
import contextlib
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import types
import warnings
from pathlib import Path
from unittest import mock


def make_template(path: Path, env: dict) -> None:
    """A repository on main with one commit "Initial commit" of README.md, git run with env."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ("init", "-q"),
        ("symbolic-ref", "HEAD", "refs/heads/main"),
        ("config", "gc.auto", "0"),
        ("config", "maintenance.auto", "false"),
        ("add", "README.md"),
        ("commit", "-q", "-m", "Initial commit"),
    ):
        if args[0] == "add":
            (path / "README.md").write_text("hello\n")
        subprocess.run(
            ["git", "-C", str(path), *args], check=True, capture_output=True, env=env
        )


def copy_repo(src: Path, dst: Path) -> None:
    """Copy a repository with symlinks kept, leaving out every `*.lock` under `.git`."""
    git_dir = os.path.join(os.path.abspath(src), ".git")

    def git_locks(directory: str, names: list) -> set:
        here = os.path.abspath(directory)
        if here != git_dir and not here.startswith(git_dir + os.sep):
            return set()
        return {name for name in names if name.endswith(".lock")}

    shutil.copytree(src, dst, symlinks=True, ignore=git_locks)


# ------------------------------------------------------------------ the git binary itself (#378)


def _find_git() -> str:
    """The git binary itself. On macOS, when the git on PATH is Apple's /usr/bin/git (an xcrun shim
    that looks the developer tools up on every call), the file `xcrun --find git` names, resolved to
    the file itself: Apple's git finds its helpers and templates through the path it was started by,
    so a link to it elsewhere loses them. Elsewhere the git PATH finds, /usr/bin/git when it has none."""
    found = shutil.which("git") or "/usr/bin/git"
    if sys.platform == "darwin" and os.path.realpath(found) == "/usr/bin/git":
        try:
            p = subprocess.run(
                ["/usr/bin/xcrun", "--find", "git"],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=60,
            )
            real = p.stdout.strip() if p.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            real = ""
        if real:
            real = os.path.realpath(real)
            if os.path.isfile(real) and os.access(real, os.X_OK):
                return real
    return found


GIT = _find_git()


# ------------------------------------------------------------------ a script run in this process (#378)

_CODE: dict = {}  # a script's path -> ((mtime_ns, size), its code)
_PYCACHE: list = []  # this process's own bytecode folder, made on first use
# The interpreter's own warning filters (a release build without -W or PYTHONWARNINGS), not the
# "default" unittest installs: a script run as its own process shows no DeprecationWarning raised
# outside __main__, so a run here must not either. First filter first.
_STOCK_WARNINGS = (
    ("default", DeprecationWarning, "__main__"),
    ("ignore", DeprecationWarning, ""),
    ("ignore", PendingDeprecationWarning, ""),
    ("ignore", ImportWarning, ""),
    ("ignore", ResourceWarning, ""),
)
_SIGNALS = tuple(
    getattr(signal, name)
    for name in ("SIGALRM", "SIGTERM", "SIGHUP", "SIGINT")
    if hasattr(signal, name)
)


def _code(path: str):
    """The script at `path` compiled once per process (again only when the file changes), without
    the __future__ flags of this module."""
    st = os.stat(path)
    stamp = (st.st_mtime_ns, st.st_size)
    cached = _CODE.get(path)
    if cached is None or cached[0] != stamp:
        with open(path, encoding="utf-8") as fh:
            cached = (stamp, compile(fh.read(), path, "exec", dont_inherit=True))
        _CODE[path] = cached
    return cached[1]


def _bytecode_folder() -> str:
    """This process's own temporary folder for the bytecode of what in-process runs import; removed
    at exit."""
    if not _PYCACHE:
        folder = tempfile.mkdtemp(prefix="harness-in-process-pycache-")
        atexit.register(shutil.rmtree, folder, True)
        _PYCACHE.append(folder)
    return _PYCACHE[0]


def _drop_imports(script: str) -> None:
    """Forget harness_lib and the script's sibling modules (a hook's branch_guard, which holds
    harness_lib's functions and exception classes), so the run imports them afresh as its own
    process would and nothing one run leaves in them reaches the next."""
    folders = {
        os.path.dirname(os.path.abspath(script)),
        os.path.dirname(os.path.realpath(script)),
    }
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if (
            name == "harness_lib"
            or name.startswith("harness_lib.")
            or (isinstance(path, str) and os.path.dirname(os.path.abspath(path)) in folders)
        ):
            del sys.modules[name]


def run_in_process(script, args=(), stdin: str = "", cwd=None, env: dict | None = None):
    """`python3 <script> <args>` run in this process instead of its own: the script's code, compiled
    once per process, as `__main__` (`__file__` the script, `sys.argv` the script then `args`, the
    script's folder first on sys.path), with `stdin` as a UTF-8 standard input, os.environ replaced
    by `env` in full (None: a copy of the current one) and `cwd` as the working directory (None:
    this one). Its stdout and stderr are captured (sys.__stdout__ and sys.__stderr__ too, so argparse
    does not size its help to this terminal), warnings filtered as the interpreter's own filters
    would, and harness_lib and the script's sibling modules imported afresh, their bytecode written
    only outside the checkout: in the environment's PYTHONPYCACHEPREFIX, else sys.pycache_prefix,
    else a temporary folder of this process's own, so importing them again is cheap even under
    PYTHONDONTWRITEBYTECODE=1.

    An object with `returncode` (SystemExit's code: None is 0, a number that status, any other a
    text written to stderr with a newline and status 1), `stdout` and `stderr`; any other exception
    propagates. Afterwards the working directory, os.environ, sys.path, sys.modules["__main__"],
    the warning filters and the handlers of SIGALRM, SIGTERM, SIGHUP and SIGINT are as they were,
    and ITIMER_REAL is off."""
    path = str(script)
    argv = [path, *(str(a) for a in args)]
    run_env = dict(os.environ) if env is None else dict(env)
    prefix = run_env.get("PYTHONPYCACHEPREFIX") or sys.pycache_prefix or _bytecode_folder()
    prefix = os.path.abspath(prefix)
    code = _code(path)
    _drop_imports(path)
    main = types.ModuleType("__main__")
    main.__dict__.update(
        __file__=path,
        __builtins__=builtins,
        __cached__=None,
        __loader__=None,
        __package__=None,
        __spec__=None,
    )
    out, err = io.StringIO(), io.StringIO()
    stdin_file = io.TextIOWrapper(io.BytesIO(stdin.encode("utf-8")), encoding="utf-8")
    status, here = 0, os.getcwd()
    handlers = {sig: signal.getsignal(sig) for sig in _SIGNALS}
    saved_main = sys.modules.get("__main__")
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, run_env, clear=True))
            stack.enter_context(mock.patch.object(sys, "stdin", stdin_file))
            stack.enter_context(mock.patch.object(sys, "argv", argv))
            script_dir = os.path.dirname(os.path.realpath(path))
            stack.enter_context(mock.patch.object(sys, "path", [script_dir, *sys.path]))
            stack.enter_context(mock.patch.object(sys, "pycache_prefix", prefix))
            stack.enter_context(mock.patch.object(sys, "dont_write_bytecode", False))
            for name, stream in (
                ("stdout", out),
                ("stderr", err),
                ("__stdout__", out),
                ("__stderr__", err),
            ):
                stack.enter_context(mock.patch.object(sys, name, stream))
            stack.enter_context(warnings.catch_warnings())
            warnings.resetwarnings()
            for action, category, module in reversed(_STOCK_WARNINGS):
                warnings.filterwarnings(action, category=category, module=module)
            if cwd is not None:
                os.chdir(str(cwd))
            sys.modules["__main__"] = main
            try:
                exec(code, main.__dict__)
            except SystemExit as exc:
                if exc.code is None or isinstance(exc.code, int):
                    status = exc.code or 0
                else:
                    err.write(f"{exc.code}\n")
                    status = 1
    finally:
        if saved_main is None:
            sys.modules.pop("__main__", None)
        else:
            sys.modules["__main__"] = saved_main
        os.chdir(here)
        if hasattr(signal, "setitimer"):
            signal.setitimer(signal.ITIMER_REAL, 0)
        for sig, handler in handlers.items():
            if handler is not None:
                signal.signal(sig, handler)
    return types.SimpleNamespace(
        returncode=status, stdout=out.getvalue(), stderr=err.getvalue()
    )
