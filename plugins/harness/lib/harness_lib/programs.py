"""The program files a Bash part runs through an interpreter, a shell, a test runner or a
wrapper, for tests_first.py (#321, #364): what the hook judges against the worktree, since what
a program writes cannot be seen. `program_files(name, args, part, cwd, exports)` lists them for
one part as `Program(path, text, kind, runner)`: `path` absolute, or None when it cannot be
known (an unexpanded variable, an unknown directory, a module on a PYTHONPATH entry the hook
cannot expand, a loader's argument that is no literal); `kind` "program", "preload", "module",
"target", "source" or "stdin"; `runner` True when a test runner is given it (a test file,
directory or config: judged by the hook's rule for runners). `note_exports(words, name, args,
exports)` records the PYTHONPATH a part exports for the parts after it. Parsing: no git, no
refusal text; the only file read is the first four bytes of one run by its path.

What is found: the first non-option word of python, node (and tsx, ts-node), deno run, bun,
ruby, perl, php and the shells when it names a program file (an extension of the family, a
path, an existing file, or a directory with an entry file: __main__.py, index.js) behind any
option, with the values of options that take one skipped (a load path, a config, a working
directory, a warning filter) unless the option is a preload (node -r/--require/--import/
--loader/--experimental-loader, ruby -r, bun --preload/-r, php -d auto_prepend_file=), whose
value is a program too; python -m (also -mX and -ImX) resolved to X.py or X/__main__.py in the
part's directory and on each PYTHONPATH entry (the part's own, env's, or exported earlier),
every place it is found; the standard library's runners of a file or module (pdb, cProfile,
profile, trace, doctest, runpy) by what they run; the test runners (pytest, unittest, node
--test, deno test, bun test, vitest, jest, mocha) by their test paths, directories and configs
(the JS configs vitest, jest and mocha execute included), a dotted module resolved like -m,
their output paths skipped, and by the part's directory when they are given no target, since
that is where they collect; and the wrappers (uv run, poetry run, pipenv run, pdm run, hatch
run, pnpm exec, npx, bunx, uvx) by what follows them, whatever global options stand before
the subcommand (uv -q run, poetry -C . run). An installed module (the standard library, a
runner) found nowhere is no program; one not found with a PYTHONPATH entry the hook cannot
expand (a variable or substitution, $PWD included: the hook's is not the part's) is not known.

#364 adds four shapes. An interpreter that reads its program from stdin (python, node, ruby,
perl and php given no program file, module or inline program, or given -, /dev/stdin; a shell
with none or -s) reads the files of its < redirections, what the commands substituted into
its here-strings and unquoted heredocs read ($(cat f), $(<f), `head f`), and else what the
parts before it in its pipeline read: their words that name a file and, for a command that
copies its input (READERS: cat, head, sed, awk, tr, grep...), a word spelled as a path, known
or not (kind "stdin"). source f and . f run f (kind "source"). A command run by its path (a
word with a /) that is a text file, with a shebang or none, runs as a script whatever it is
called (kind "program", alongside what its name would run); a binary (ELF, Mach-O), a
directory or a path that does not exist is no program file. An inline program (-c, -e, -p,
-r, a heredoc on stdin) runs what it loads: python's import, from ... import,
importlib.import_module, __import__ and runpy.run_module resolved like -m (with __init__.py
for a package), exec or eval of open(f) and runpy.run_path(f); node's require(), import() and
import ... from on a relative or absolute specifier; ruby's require (a path), require_relative
and load; perl's do and require (a path); php's include and require; each a literal, or a
path that cannot be known. The files the commands substituted into an inline program or eval
read are its program too (python3 -c "$(cat f)").

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import ast
import os
import re
import stat
import sys
from typing import NamedTuple, Optional

from harness_lib.shellwalk import ASSIGNMENT, PYTHON, SHELLS, command_words, split_command, substitutions

PY_EXT = {".py", ".pyw", ".pyc"}
JS_EXT = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".jsx", ".tsx"}
RB_EXT = {".rb"}
PL_EXT = {".pl", ".pm", ".t"}
PHP_EXT = {".php", ".phtml", ".phar"}
SH_EXT = {".sh", ".bash", ".zsh", ".ksh"}
PROGRAM_EXT = PY_EXT | JS_EXT | RB_EXT | PL_EXT | PHP_EXT | SH_EXT

RUBY = re.compile(r"j?ruby[0-9.]*$")
PERL = re.compile(r"perl[0-9.]*$")
PHP = re.compile(r"php[0-9.]*$")
MODULE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
NODE_LIKE = {"node", "nodejs", "tsx", "ts-node", "ts-node-esm", "esno", "tsm"}
RUNNERS = {"pytest": "pytest", "py.test": "pytest", "vitest": "vitest", "jest": "jest", "mocha": "mocha"}
RUN_WRAPPERS = {"uv": {"run"}, "poetry": {"run"}, "pipenv": {"run"}, "pdm": {"run"}, "hatch": {"run"}, "pnpm": {"exec", "dlx"}}
EXEC_WRAPPERS = {"npx", "bunx", "pnpx", "uvx"}
WRAPPER_VALUES = {
    "uv": set(
        "--with --with-requirements --with-editable --python -p --extra --group --only-group --project "
        "--directory --env-file --index --default-index --index-url --extra-index-url --find-links -f "
        "--cache-dir --config-file --color --exclude-newer --python-preference --link-mode --resolution "
        "--prerelease --index-strategy --keyring-provider --package".split()
    ),
    "uvx": set("--with --python -p --from --index --default-index --cache-dir --config-file --color".split()),
    "poetry": {"-C", "--directory", "-P", "--project"},
    "npx": set("-p --package -c --call --shell --loglevel --workspace -w --userconfig --prefix --registry --cache".split()),
    "bunx": {"-p", "--package"},
    "pnpm": {"-C", "--dir", "--filter", "-F", "--workspace-root"},
}
DENO_SUBCOMMANDS = set(
    "add bench bundle cache check compile completions coverage doc eval fmt info init install jupyter lint "
    "lsp outdated publish remove repl run serve task test types uninstall upgrade vendor".split()
)
BUN_SUBCOMMANDS = set(
    "add build create i init install link outdated patch pm publish remove repl run test uninstall unlink "
    "update upgrade x".split()
)
# Modules of the standard library that run a program file or a module they are given.
FILE_RUNNERS = {"pdb", "cProfile", "profile", "trace", "doctest"}
# Python 3.10 lists the standard library itself; before that, what a loop is likely to run.
STDLIB = frozenset(
    "__future__ abc argparse ast asyncio base64 collections compileall cProfile csv dataclasses datetime "
    "decimal difflib dis doctest email ensurepip enum filecmp fnmatch fractions functools gettext glob gzip "
    "hashlib heapq hmac html http idlelib importlib inspect io itertools json keyword lib2to3 linecache "
    "locale logging math mimetypes multiprocessing operator os pathlib pdb pickle pickletools platform "
    "pprint profile pstats py_compile pyclbr pydoc queue quopri random re runpy sched secrets shlex shutil "
    "signal site smtplib socket sqlite3 ssl statistics string struct subprocess sys sysconfig tabnanny "
    "tarfile tempfile textwrap this threading time timeit tkinter token tokenize trace traceback turtle "
    "turtledemo types typing unicodedata unittest urllib uu uuid venv warnings weakref webbrowser xml "
    "xmlrpc zipapp zipfile zipimport zoneinfo".split()
) | frozenset(getattr(sys, "stdlib_module_names", ()))
# Installed, not the worktree's: these never count as a module the hook cannot find.
KNOWN_INSTALLED = STDLIB | frozenset(
    "pip setuptools wheel pytest py coverage tox nox black ruff mypy flake8 pylint isort build twine "
    "pre_commit sphinx IPython jupyter virtualenv pipx uv poetry pdm hatch nuitka Cython numpy pandas "
    "django flask uvicorn gunicorn celery alembic".split()
)
# Commands that copy what they read to their output, filtered or not.
READERS = frozenset(
    "cat head tail sed gsed awk gawk tr cut grep egrep fgrep sort uniq tac rev nl fold expand paste "
    "iconv column strings base64 xxd od dd tee less more fmt pr".split()
)
STDIN_FILES = {"/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"}
# The first bytes of a binary the system runs (ELF; Mach-O, thin and universal, either order).
MAGIC = {b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
         b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
JS_CALL = re.compile(r"(?<![\w.$])(?:require|import)\s*\(([^)]*)")
JS_FROM = re.compile(r"""(?<![\w.$])(?:import|export)\s*(?:[\w$*{}\s,]+?\s*from\s*)?(["'])(.*?)\1""")
JS_LITERAL = re.compile(r"""\s*(["'`])(.*?)\1\s*$""")
# ruby's, perl's and php's loaders: the call, its literal argument, the character after the
# call when there is none, and how an argument the hook cannot know starts (ARGV[0], $f).
SCRIPT_LOADERS = {
    "ruby": (re.compile(r"(?<![\w.:])(require_relative|require|load)\b[\s(]*(?:(['\"])(.*?)\2)?(.?)"), "[A-Za-z_$@]"),
    "perl": (re.compile(r"(?<![\w>:$])(do|require)\b[\s(]*(?:(['\"])(.*?)\2)?(.?)"), "[$@]"),
    "php": (re.compile(r"(?<![\w>:$])(include|require)(?:_once)?\b[\s(]*(?:(['\"])(.*?)\2)?(.?)"), "[$]"),
}


class Program(NamedTuple):
    path: Optional[str]
    text: str
    kind: str
    runner: bool = False


class Spec:
    """How one interpreter spells its command line. `letters`: short options whose value is
    the rest of the cluster or the next word (ruby -Ilib, -I lib); `values`: long or exact
    options taking a separate value (`--opt=value` is one word); `preloads` and `preload_letters`:
    options whose value is a program run first; `inline`: letters of an inline program, which
    `stops` the search when the words after it belong to the program (python -c, sh -c);
    `module_letters`/`module_long`: python -m; `program_letters`: an option whose value is the
    program (php -f); `entries`: the entry files a directory runs as; `cluster`: short options
    stack (-wIlib) rather than stand alone (node -r x); `runner_flags`: node --test; `family`:
    the language of its inline programs; `stdin`: given no program it reads one from stdin, as
    it does after one of `stdin_letters` (sh -s)."""

    def __init__(self, exts, **kw) -> None:
        self.exts = exts
        self.family = kw.get("family", "")
        self.stdin = kw.get("stdin", False)
        self.stdin_letters = kw.get("stdin_letters", "")
        self.letters = kw.get("letters", "")
        self.values = set(kw.get("values", ()))
        self.preloads = set(kw.get("preloads", ()))
        self.preload_letters = kw.get("preload_letters", "")
        self.inline = kw.get("inline", "")
        self.inline_long = set(kw.get("inline_long", ()))
        self.stops = kw.get("stops", False)
        self.module_letters = kw.get("module_letters", "")
        self.module_long = set(kw.get("module_long", ()))
        self.program_letters = kw.get("program_letters", "")
        self.define_letters = kw.get("define_letters", "")
        self.entries = tuple(kw.get("entries", ()))
        self.cluster = kw.get("cluster", True)
        self.plus = kw.get("plus", False)
        self.runner_flags = set(kw.get("runner_flags", ()))


PYTHON_SPEC = Spec(
    PY_EXT, letters="WXcmQ", inline="c", stops=True, module_letters="m", family="python", stdin=True,
    values={"--check-hash-based-pycs"}, entries=("__main__.py",),
)
NODE_PRELOADS = {"-r", "--require", "--import", "--loader", "--experimental-loader"}
NODE_SPEC = Spec(
    JS_EXT, cluster=False, preloads=NODE_PRELOADS, inline_long={"-e", "--eval", "-p", "--print"},
    family="node", stdin=True,
    runner_flags={"--test"}, entries=("index.js", "index.mjs", "index.cjs", "index.ts"),
    values=set(
        "-C --conditions --input-type --env-file --env-file-if-exists --test-reporter "
        "--test-reporter-destination --test-name-pattern --test-skip-pattern --test-concurrency "
        "--test-timeout --test-shard --test-coverage-include --test-coverage-exclude --title "
        "--watch-path --inspect-port --redirect-warnings --heapsnapshot-signal --diagnostic-dir "
        "--openssl-config --icu-data-dir --unhandled-rejections --dns-result-order --secure-heap "
        "--stack-trace-limit --max-old-space-size --max-http-header-size --tsconfig -P --project "
        "--compiler --ignore -I --transpiler --cwd".split()
    ),
)
DENO_SPEC = Spec(
    JS_EXT, cluster=False, preloads={"--preload"}, entries=("index.ts", "index.js", "mod.ts"),
    values=set(
        "--config -c --import-map --lock --cert --location --seed --v8-flags --env-file --env --ext "
        "--node-modules-dir --vendor".split()
    ),
)
BUN_SPEC = Spec(
    JS_EXT, cluster=False, preloads={"--preload", "-r"}, inline_long={"-e", "--eval", "-p", "--print"}, family="node",
    entries=("index.ts", "index.js", "index.mjs"),
    values=set(
        "--cwd --timeout --config -c --env-file --port --reporter --reporter-outfile -t --test-name-pattern "
        "--rerun-each --coverage-dir --coverage-reporter --conditions --define -d --loader -l --target "
        "--format --outdir --outfile --shell --max-http-header-size --bail".split()
    ),
)
RUBY_SPEC = Spec(
    RB_EXT, letters="IrCEFKe", preload_letters="r", inline="e", family="ruby", stdin=True,
    values={"--encoding", "--external-encoding", "--internal-encoding", "--disable", "--enable"},
)
PERL_SPEC = Spec(PL_EXT, letters="IMmeE", inline="eE", family="perl", stdin=True)
PHP_SPEC = Spec(
    PHP_EXT, letters="cdfFBREzStr", inline="r", stops=True, program_letters="fF", define_letters="d",
    family="php", stdin=True,
)
SHELL_SPEC = Spec(
    SH_EXT, letters="oOc", inline="c", stops=True, plus=True, values={"--rcfile", "--init-file"},
    family="shell", stdin=True, stdin_letters="s",
)
PHP_PRELOAD_KEYS = {"auto_prepend_file", "auto_append_file"}
# The standard library's runners: options with values, the module option, and the program.
STDLIB_SPECS = {
    "pdb": Spec(PY_EXT, letters="cm", module_letters="m"),
    "cProfile": Spec(PY_EXT, letters="osm", module_letters="m"),
    "profile": Spec(PY_EXT, letters="osm", module_letters="m"),
    "trace": Spec(
        PY_EXT, letters="Cfm", module_letters="m", module_long={"--module"},
        values={"--coverdir", "--file", "--ignore-module", "--ignore-dir"},
    ),
    "doctest": Spec(PY_EXT, letters="o"),
}
# Test runners: options with values that are not test paths (reports, temp and cache
# directories, patterns), the options whose value is a test directory or config (judged; the
# configs of vitest, jest and mocha are JS or TS modules the runner executes, deno's and
# bun's are data), the preloads, and the words that are subcommands, not paths.
RUNNER_SPECS = {
    "pytest": {
        "values": set(
            "-k -m -p -o -W -c --rootdir --confcutdir --junitxml --junit-xml --basetemp --tb --maxfail "
            "--durations --durations-min -n --numprocesses --dist --cov --cov-report --cov-config "
            "--log-level --log-file --log-cli-level --log-format --log-date-format --timeout --deselect "
            "--ignore --ignore-glob --override-ini --import-mode --color --code-highlight --pdbcls -r "
            "--result-log --cache-dir --html --css --self-contained-html --last-failed-no-failures "
            "--doctest-glob --doctest-report --assert --show-capture --capture --rootpath".split()
        ),
        "dirs": {"-c", "--rootdir", "--confcutdir"},
        "preloads": set(),
        "skip": set(),
        "modules": True,
    },
    "unittest": {
        "values": {"-k", "-p", "--pattern", "-s", "--start-directory", "-t", "--top-level-directory"},
        "dirs": {"-s", "--start-directory", "-t", "--top-level-directory"},
        "preloads": set(),
        "skip": {"discover"},
        "modules": True,
    },
    "deno": {
        "values": set(
            "--junit-path --filter --reporter --config -c --import-map --coverage --lock --cert --location "
            "--seed --v8-flags --env-file --env --ext --node-modules-dir --vendor".split()
        ),
        "dirs": set(),
        "preloads": {"--preload"},
        "skip": set(),
        "modules": False,
    },
    "bun": {
        "values": set(
            "--timeout --reporter --reporter-outfile -t --test-name-pattern --rerun-each --coverage-dir "
            "--coverage-reporter --config -c --env-file --port --bail --cwd".split()
        ),
        "dirs": {"--cwd"},
        "preloads": {"--preload", "-r"},
        "skip": set(),
        "modules": False,
    },
    "vitest": {
        "values": set(
            "--reporter --outputFile --root -r --dir --testNamePattern -t --pool --maxWorkers "
            "--minWorkers --shard --project --environment --mode --coverage.reportsDirectory "
            "--coverage.reporter --coverage.provider --exclude --testTimeout --hookTimeout --retry "
            "--sequence.seed --bail".split()
        ),
        "dirs": {"--root", "-r", "--dir", "--config", "-c"},
        "preloads": set(),
        "skip": {"run", "watch", "dev", "bench", "related", "list", "init"},
        "modules": False,
    },
    "jest": {
        "values": set(
            "--rootDir --roots --testPathPattern -t --testNamePattern --outputFile --reporters "
            "--coverageDirectory --maxWorkers -w --selectProjects --testTimeout --shard --cacheDirectory".split()
        ),
        "dirs": {"--rootDir", "--roots", "--config", "-c"},
        "preloads": set(),
        "skip": set(),
        "modules": False,
    },
    "mocha": {
        "values": set(
            "--reporter -R --reporter-option -O --require -r --file --spec --timeout -t --package "
            "--extension --ignore --exclude --grep -g --fgrep -f --retries --slow -s --ui -u -n --node-option".split()
        ),
        "dirs": {"--config"},
        "preloads": {"--require", "-r"},
        "skip": set(),
        "modules": False,
    },
}


# ---------- words ----------


def resolve(word: str, cwd: str | None) -> str | None:
    """`word` as an absolute, normalized path; None when it cannot be known (an unexpanded
    variable or substitution, a relative path in an unknown directory)."""
    if not word:
        return None
    path = os.path.expandvars(os.path.expanduser(word))
    if "$" in path or "`" in path:
        return None
    if not os.path.isabs(path):
        if cwd is None:
            return None
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


def extension(word: str) -> str:
    base = os.path.basename(word.rstrip("/"))
    return "" if base.startswith(".") else os.path.splitext(base)[1]


def path_like(word: str) -> bool:
    """A word spelled as a path: a program extension, a separator, ~ or a variable."""
    return bool(word) and (
        extension(word) in PROGRAM_EXT or "/" in word or word.startswith("~") or "$" in word or "`" in word
    )


def program_word(spec: Spec, word: str, cwd: str | None) -> Program | None:
    """`word` as the program an interpreter runs, or None when it is no program file (an
    option's value the tables do not know): a directory with one of `spec.entries`, a path,
    an existing file. /dev/stdin is stdin."""
    if not word or word == "-":
        return None
    full = resolve(word, cwd)
    if full is None:
        return Program(None, word, "program") if path_like(word) else None
    if full in STDIN_FILES:
        return Program(None, spec.family, "stdin-program")
    if os.path.isdir(full):
        for entry in spec.entries:
            if os.path.isfile(os.path.join(full, entry)):
                return Program(os.path.join(full, entry), word, "program")
        return None
    if path_like(word) or os.path.isfile(full):
        return Program(full, word, "program")
    return None


def preload_word(word: str, cwd: str | None, found: list) -> None:
    """A preload option's value: a program when it names a file (by extension or on disk);
    a bare module name (dotenv/config) is not."""
    if not word:
        return
    full = resolve(word, cwd)
    if full is None:
        if path_like(word):
            found.append(Program(None, word, "preload"))
    elif extension(word) in PROGRAM_EXT or os.path.isfile(full):
        found.append(Program(full, word, "preload"))


# ---------- python modules ----------


def part_pythonpath(words: list[str], nargs: int, exports: dict) -> str | None:
    """The PYTHONPATH set on the part (a leading assignment or env's), else the exported one."""
    value = exports.get("PYTHONPATH")
    for w in words[: max(len(words) - nargs - 1, 0)]:
        if w.startswith("PYTHONPATH="):
            value = w[len("PYTHONPATH="):]
    return value


def note_exports(words: list[str], name: str, args: list[str], exports: dict) -> None:
    """Records a PYTHONPATH the part `export`s, assigns alone or `unset`s, for the parts
    after it in the same command."""
    if name == "export":
        for a in args:
            if a.startswith("PYTHONPATH="):
                exports["PYTHONPATH"] = a[len("PYTHONPATH="):]
    elif name == "unset" and "PYTHONPATH" in args:
        exports.pop("PYTHONPATH", None)
    elif not name:
        for w in words:
            if ASSIGNMENT.match(w) and w.startswith("PYTHONPATH="):
                exports["PYTHONPATH"] = w[len("PYTHONPATH="):]


def python_roots(cwd: str | None, pythonpath: str | None) -> list:
    """Where python -m looks: the part's directory, then each PYTHONPATH entry (an empty
    one is the directory); None stands for one that cannot be known: a relative entry in
    an unknown directory, or one with a variable or a substitution, which the hook does
    not expand ($PWD in the hook's environment is the session's directory, not the
    part's after a cd)."""
    roots: list = [cwd]
    if pythonpath is not None:
        for e in pythonpath.split(":"):
            if not e:
                roots.append(cwd)
            elif "$" in e or "`" in e:
                roots.append(None)
            else:
                roots.append(resolve(e, cwd))
    seen: list = []
    for r in roots:
        if r not in seen:
            seen.append(r)
    return seen


def module_candidates(module: str, roots: list) -> tuple[list[str], bool]:
    """(the files `module` resolves to on `roots`, whether a root is unknown)."""
    if not MODULE_NAME.match(module):
        return [], False
    rel = module.replace(".", os.sep)
    found: list[str] = []
    unknown = False
    for root in roots:
        if root is None:
            unknown = True
            continue
        for c in (os.path.join(root, rel + ".py"), os.path.join(root, rel, "__main__.py")):
            if os.path.isfile(c) and c not in found:
                found.append(c)
    return found, unknown


def module_programs(module: str, rest: list[str], cwd: str | None, roots: list, found: list, depth: int) -> None:
    """python -m `module` `rest`: the module's file where it is found; else an installed
    module, judged by what it runs when it runs something."""
    if not module:
        return
    files, unknown = module_candidates(module, roots)
    if files:
        found.extend(Program(f, f"-m {module}", "module") for f in files)
        return
    if unknown and module.split(".")[0] not in KNOWN_INSTALLED:
        found.append(Program(None, f"-m {module}", "module"))
        return
    if module in STDLIB_SPECS:
        interpreter_programs(STDLIB_SPECS[module], rest, cwd, roots, found, depth + 1)
    elif module == "runpy":
        for w in rest:
            if w != "--" and not w.startswith("-"):
                module_programs(w, [], cwd, roots, found, depth + 1)
                break
    elif module in ("unittest", "pytest"):
        runner_targets(module, rest, cwd, roots, found)


# ---------- interpreters ----------


def take_value(rest: str, args: list[str], k: int) -> tuple[str, int]:
    """A short option's value: the rest of its cluster, else the next word (consumed)."""
    if rest:
        return rest, k
    return (args[k], k + 1) if k < len(args) else ("", k)


def interpreter_programs(spec: Spec, args: list[str], cwd: str | None, roots: list, found: list, depth: int = 0) -> None:
    """The programs an interpreter spelled by `spec` runs given `args`: its preloads, its
    module (python -m) or the first non-option word naming a program file; the words after
    the program are its arguments; an inline program runs what it loads (inline_programs).
    node --test turns what is found into runner targets. Given none of these, or -, one that
    reads its program from stdin (spec.stdin) leaves a "stdin-program" mark for program_files."""
    if depth > 4:
        return
    start = len(found)
    inline = runner = ran = reads = False
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            if k < len(args) and not inline:
                prog = program_word(spec, args[k], cwd)
                if prog:
                    found.append(prog)
                    ran = True
            break
        if a == "-" or not a:
            reads, ran = a == "-", True
            break
        if a.startswith("--") or (not spec.cluster and a.startswith("-")):
            opt, eq, val = a.partition("=")
            if opt in spec.preloads:
                if not eq:
                    val, k = take_value("", args, k)
                preload_word(val, cwd, found)
            elif opt in spec.inline_long:
                inline = True
                if not eq:
                    val, k = take_value("", args, k)
                inline_programs(spec.family, val, cwd, roots, found)
            elif opt in spec.module_long:
                if not eq:
                    val, k = take_value("", args, k)
                module_programs(val, args[k:], cwd, roots, found, depth)
                ran = True
                break
            elif opt in spec.runner_flags:
                runner = True
            elif opt in spec.values and not eq:
                k += 1
            continue
        if a.startswith("-") or (spec.plus and a.startswith("+")):
            letters = a[1:]
            stop = False
            for i, ch in enumerate(letters):
                rest = letters[i + 1 :]
                if ch in spec.module_letters:
                    val, k = take_value(rest, args, k)
                    module_programs(val, args[k:], cwd, roots, found, depth)
                    stop = ran = True
                    break
                if ch in spec.inline:
                    inline = True
                    val, k = take_value(rest, args, k)
                    inline_programs(spec.family, val, cwd, roots, found)
                    stop = spec.stops
                    break
                if ch in spec.stdin_letters:
                    stop = reads = True
                    break
                if ch in spec.preload_letters:
                    val, k = take_value(rest, args, k)
                    preload_word(val, cwd, found)
                    break
                if ch in spec.program_letters:
                    val, k = take_value(rest, args, k)
                    prog = program_word(spec, val, cwd)
                    if prog:
                        found.append(prog)
                    stop = ran = True
                    break
                if ch in spec.define_letters:
                    val, k = take_value(rest, args, k)
                    key, _, value = val.partition("=")
                    if key in PHP_PRELOAD_KEYS:
                        preload_word(value, cwd, found)
                    break
                if ch in spec.letters:
                    _, k = take_value(rest, args, k)
                    break
            if stop:
                break
            continue
        if inline:
            break
        if runner:
            target_word(a, cwd, roots, found, False)
            continue
        prog = program_word(spec, a, cwd)
        if prog:
            found.append(prog)
            ran = True
            break
    if runner:
        implicit_target(cwd, found, start)
        found[start:] = [Program(p.path, p.text, p.kind, True) for p in found[start:]]
    elif spec.stdin and not inline and (reads or not ran):
        found.append(Program(None, spec.family, "stdin-program"))


# ---------- what an inline program loads ----------


def inline_programs(family: str, code: str, cwd: str | None, roots: list, found: list) -> None:
    """What the inline program `code` (python -c, node -e, ruby -e, perl -e, php -r, sh -c)
    runs besides itself: the files the commands substituted into it read (python3 -c "$(cat
    f)" runs f), and what it loads (loads)."""
    for feed in substitutions(code):
        feed_reads(feed, cwd, found)
    loads(family, code, cwd, roots, found)


def loads(family: str, code: str, cwd: str | None, roots: list, found: list) -> None:
    """The files and modules the program `code` in `family`'s language loads and runs."""
    if family == "python":
        python_loads(code, roots, cwd, found)
    elif family == "node":
        for m in JS_CALL.finditer(code):  # require(x), import(x)
            literal = JS_LITERAL.match(m.group(1))
            if literal and "${" not in literal.group(2):
                js_file(literal.group(2), cwd, found)
            elif m.group(1).strip():
                found.append(Program(None, m.group(1).strip(), "program"))
        for m in JS_FROM.finditer(code):  # import x from "y", import "y"
            js_file(m.group(2), cwd, found)
    elif family in SCRIPT_LOADERS:
        pattern, unknown = SCRIPT_LOADERS[family]
        for m in pattern.finditer(code):
            call, literal = m.group(1), m.group(3)
            if literal is None:
                if re.match(unknown, m.group(4)):  # load ARGV[0], do $f, include $argv[1]
                    found.append(Program(None, code[m.start() :].split(";")[0].strip()[:80], "program"))
            # require "json" and do "Foo.pm" search the load path: a library, not a file here
            elif literal and (
                call in ("load", "require_relative") or family == "php" or literal.startswith(("/", "./", "../", "~"))
            ):
                full = resolve(literal, cwd)
                if full is not None and family == "ruby" and not extension(literal) and os.path.isfile(full + ".rb"):
                    full += ".rb"
                found.append(Program(full, literal, "program"))


def python_loads(code: str, roots: list, cwd: str | None, found: list) -> None:
    """python's imports (import, from ... import, importlib.import_module, __import__,
    runpy.run_module) resolved on `roots`, and the files it runs: exec or eval of what
    open(f) or Path(f) reads, runpy.run_path(f); exec of sys.stdin reads its program from
    stdin. An argument that is no literal cannot be known. Code that does not parse loads
    nothing: it does not run."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                import_programs(alias.name, roots, found)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            import_programs(node.module, roots, found)
            for alias in node.names:  # from tools import gen imports tools/gen.py
                import_programs(f"{node.module}.{alias.name}", roots, found, quiet=True)
        elif isinstance(node, ast.Call) and node.args:
            name = called(node)
            calls = [node] if name in ("import_module", "__import__", "run_module", "run_path") else []
            if name in ("exec", "eval", "execfile"):
                inner = list(ast.walk(node))
                calls = [c for c in inner if isinstance(c, ast.Call) and called(c) in ("open", "Path") and c.args]
                if any(isinstance(n, ast.Attribute) and n.attr == "stdin" for n in inner):
                    found.append(Program(None, "python", "stdin-program"))  # exec(sys.stdin.read())
            for call in calls:
                arg = call.args[0]
                if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                    found.append(Program(None, ast.get_source_segment(code, arg) or name, "program"))
                elif arg.value and called(call) in ("run_path", "open", "Path"):
                    found.append(Program(resolve(arg.value, cwd), arg.value, "program"))
                elif arg.value:
                    import_programs(arg.value, roots, found)


def called(call) -> str:
    """The name a call calls: f for f(), the attribute for x.f()."""
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def import_programs(module: str, roots: list, found: list, quiet: bool = False) -> None:
    """The files `import module` runs on `roots`: each package's __init__.py and the module,
    every place found. Found nowhere, it is installed, or not known when a root is not known
    and it is not installed (unless `quiet`: from x import name, where name need not be a
    module)."""
    if not MODULE_NAME.match(module):
        return
    names = module.split(".")
    files, unknown = [], False
    for root in roots:
        if root is None:
            unknown = True
            continue
        for i in range(1, len(names) + 1):
            base = os.path.join(root, *names[:i])
            for f in (base + ".py", os.path.join(base, "__init__.py")):
                if os.path.isfile(f) and f not in files:
                    files.append(f)
    found.extend(Program(f, module, "module") for f in files)
    if not files and unknown and not quiet and names[0] not in KNOWN_INSTALLED:
        found.append(Program(None, module, "module"))


def js_file(specifier: str, cwd: str | None, found: list) -> None:
    """A relative or absolute specifier node loads (a bare one is a package or a built-in):
    the file it resolves to, by itself, with an extension or as a directory's index.js."""
    if not specifier.startswith(("./", "../", "/")):
        return
    full = resolve(specifier, cwd)
    if full is not None:
        for f in [full] + [full + e for e in (".js", ".mjs", ".cjs", ".json")] + [os.path.join(full, "index.js")]:
            if os.path.isfile(f):
                full = f
                break
    found.append(Program(full, specifier, "program"))


# ---------- runners ----------


def target_word(word: str, cwd: str | None, roots: list, found: list, modules: bool) -> None:
    """A test runner's test path (a file or directory, spelled as a path or on disk), or,
    for the python runners, a dotted module resolved like -m."""
    if not word or word == "-":
        return
    full = resolve(word, cwd)
    if path_like(word) or (full is not None and os.path.exists(full)):
        found.append(Program(full, word, "target", True))
    elif modules and MODULE_NAME.match(word):
        files, unknown = module_candidates(word, roots)
        found.extend(Program(f, word, "target", True) for f in files)
        if not files and unknown and word.split(".")[0] not in KNOWN_INSTALLED:
            found.append(Program(None, word, "target", True))


def runner_targets(kind: str, args: list[str], cwd: str | None, roots: list, found: list) -> None:
    """What the test runner `kind` is given: test paths, directories and configs (judged
    for being in the worktree), its preloads; output paths and patterns skipped."""
    spec = RUNNER_SPECS[kind]
    start = len(found)
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            for w in args[k:]:
                target_word(w, cwd, roots, found, spec["modules"])
            break
        if a.startswith("-") and a != "-":
            opt, eq, val = a.partition("=") if a.startswith("--") else (a, "", "")
            if opt in spec["dirs"]:
                if not eq:
                    val, k = take_value("", args, k)
                target_word(val, cwd, roots, found, False)
            elif opt in spec["preloads"]:
                if not eq:
                    val, k = take_value("", args, k)
                preload_word(val, cwd, found)
            elif opt in spec["values"] and not eq:
                k += 1
            continue
        if a in spec["skip"]:
            continue
        target_word(a, cwd, roots, found, spec["modules"])
    implicit_target(cwd, found, start)
    found[start:] = [Program(p.path, p.text, p.kind, True) for p in found[start:]]


def implicit_target(cwd: str | None, found: list, start: int) -> None:
    """A runner given no test path, directory or config collects from the directory the
    part runs in: that directory is its target (None when the directory is not known)."""
    if not any(p.kind == "target" for p in found[start:]):
        found.append(Program(cwd, ".", "target", True))


# ---------- wrappers and dispatch ----------


def wrapper_subcommand(name: str, args: list[str]) -> int:
    """The index of the first word of `args` that is not one of wrapper `name`'s global
    options (uv -q run, poetry -C . run, pnpm --silent exec): where its subcommand stands."""
    values = WRAPPER_VALUES.get(name, set())
    k = 0
    while k < len(args) and args[k].startswith("-") and args[k] != "-":
        k += 2 if args[k] in values else 1
    return k


def wrapper_programs(name: str, args: list[str], cwd: str | None, roots: list, found: list, depth: int) -> None:
    """uv run, npx and the like: the command after the wrapper's options, or a program file
    given directly (uv run script.py)."""
    values = WRAPPER_VALUES.get(name, set())
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            if k < len(args):
                dispatch(args[k], args[k + 1 :], cwd, roots, found, depth + 1)
            return
        if a.startswith("-") and a != "-":
            if a in values:
                k += 1
            continue
        if not dispatch(a, args[k:], cwd, roots, found, depth + 1):
            prog = program_word(PYTHON_SPEC if name in RUN_WRAPPERS or name == "uvx" else NODE_SPEC, a, cwd)
            if prog:
                found.append(prog)
        return


def deno_programs(args: list[str], cwd: str | None, roots: list, found: list, depth: int) -> None:
    if not args:
        return
    sub = args[0]
    if sub in ("run", "serve"):
        interpreter_programs(DENO_SPEC, args[1:], cwd, roots, found, depth)
    elif sub in ("test", "bench"):
        runner_targets("deno", args[1:], cwd, roots, found)
    elif sub not in DENO_SUBCOMMANDS:
        interpreter_programs(DENO_SPEC, args, cwd, roots, found, depth)


def bun_programs(args: list[str], cwd: str | None, roots: list, found: list, depth: int) -> None:
    if not args:
        return
    sub = args[0]
    if sub == "run":
        interpreter_programs(BUN_SPEC, args[1:], cwd, roots, found, depth)
    elif sub == "test":
        runner_targets("bun", args[1:], cwd, roots, found)
    elif sub == "x":
        wrapper_programs("bunx", args[1:], cwd, roots, found, depth)
    elif sub not in BUN_SUBCOMMANDS:
        interpreter_programs(BUN_SPEC, args, cwd, roots, found, depth)


def dispatch(word: str, args: list[str], cwd: str | None, roots: list, found: list, depth: int = 0) -> bool:
    """Judges the command `word` `args`; False when it is no interpreter, runner or wrapper."""
    name = os.path.basename(word)
    if depth > 4 or not name:
        return False
    if PYTHON.match(name):
        interpreter_programs(PYTHON_SPEC, args, cwd, roots, found, depth)
    elif name in NODE_LIKE:
        interpreter_programs(NODE_SPEC, args, cwd, roots, found, depth)
    elif name == "deno":
        deno_programs(args, cwd, roots, found, depth)
    elif name == "bun":
        bun_programs(args, cwd, roots, found, depth)
    elif RUBY.match(name):
        interpreter_programs(RUBY_SPEC, args, cwd, roots, found, depth)
    elif PERL.match(name):
        interpreter_programs(PERL_SPEC, args, cwd, roots, found, depth)
    elif PHP.match(name):
        interpreter_programs(PHP_SPEC, args, cwd, roots, found, depth)
    elif name in SHELLS:
        interpreter_programs(SHELL_SPEC, args, cwd, roots, found, depth)
    elif name in RUNNERS:
        runner_targets(RUNNERS[name], args, cwd, roots, found)
    elif name in RUN_WRAPPERS:
        k = wrapper_subcommand(name, args)
        if k < len(args) and args[k] in RUN_WRAPPERS[name]:
            wrapper_programs(name, args[k + 1 :], cwd, roots, found, depth)
    elif name in EXEC_WRAPPERS:
        wrapper_programs(name, args, cwd, roots, found, depth)
    else:
        return False
    return True


# ---------- stdin, source and scripts run by their path ----------


def stdin_reads(part, cwd: str | None, found: list) -> None:
    """What `part` reads on stdin besides a pipe: the files of its < redirections and what the
    commands substituted into its here-strings and unquoted heredocs read."""
    for word in part.inputs:
        found.append(Program(resolve(word, cwd), word, "stdin"))
    for feed in part.feeds:
        feed_reads(feed, cwd, found)


def stage_reads(part, cwd: str | None, found: list) -> None:
    """What `part`, before an interpreter in a pipeline or substituted into its stdin, writes
    out: the files its words name and, for one of READERS, a word spelled as a path that does
    not exist or cannot be known (not an option, a pattern or a sed script), and its stdin."""
    words = command_words(part.words)
    reader = bool(words) and os.path.basename(words[0]) in READERS
    for word in words[1:]:
        if word.startswith("-"):
            continue
        full = resolve(word, cwd)
        if full is None and reader:
            found.append(Program(None, word, "stdin"))
        elif full is not None and (
            (os.path.exists(full) and not os.path.isdir(full))
            or (reader and (extension(word) in PROGRAM_EXT or word.startswith(("/", "~", "./", "../"))))
        ):
            found.append(Program(full, word, "stdin"))
    stdin_reads(part, cwd, found)


def feed_reads(feed: str, cwd: str | None, found: list) -> None:
    """What the command `feed`, substituted into a text ($(cat f), `head f`), reads."""
    text = feed.strip()
    if text.startswith("<") and not text.startswith("<<"):
        text = "cat " + text  # $(< f) is $(cat f)
    for part in split_command(text):
        if not part.marker:
            stage_reads(part, cwd, found)


def script_run(word: str, cwd: str | None) -> Program | None:
    """The script a command run by its path (`word` holds a /) is: a text file, with a shebang
    or none, whatever it is called; None for a binary (ELF, Mach-O), a directory or a path that
    does not exist, and for a command substitution's output (the shell's own text in a
    heredoc a child shell reads). Only a regular file is read, and only its first four bytes."""
    if "/" not in word or "$(" in word or "`" in word:
        return None
    full = resolve(word, cwd)
    if full is None:
        return Program(None, word, "program")
    try:
        mode = os.stat(full).st_mode
    except OSError:  # it does not exist (a virtual environment's python with none there)
        return None
    if stat.S_ISDIR(mode):
        return None
    if stat.S_ISREG(mode):
        try:
            fd = os.open(full, os.O_RDONLY | os.O_NONBLOCK)
            try:
                head = os.read(fd, 4)
            finally:
                os.close(fd)
        except OSError:  # unreadable: no binary the hook knows
            head = b""
        if head in MAGIC:
            return None
    return Program(full, word, "program")


def program_files(name: str, args: list[str], part, cwd: str | None, exports: dict) -> list:
    """The programs the Part `part` (command `name`, its `args`) runs in `cwd`, as Program
    records; [] for a command that runs none. One that reads its program from stdin is
    given what it reads: stdin_reads, else the pipe's (stage_reads), and its heredocs'
    loads."""
    found: list = []
    roots = python_roots(cwd, part_pythonpath(part.words, len(args), exports))
    script = script_run(part.words[-len(args) - 1] if name else "", cwd)
    if script:
        found.append(script)
    if name in ("source", "."):
        plain = args[1:] if args[:1] == ["--"] else args
        full = resolve(plain[0], cwd) if plain and plain[0] not in ("", "-") else ""
        if full is None or (full and not os.path.isdir(full)):
            found.append(Program(full, plain[0], "source"))
    elif name == "eval":
        for feed in substitutions(" ".join(args)):
            feed_reads(feed, cwd, found)
    else:
        dispatch(name, args, cwd, roots, found)
    marks = [p for p in found if p.kind == "stdin-program"]
    if marks:
        found = [p for p in found if p.kind != "stdin-program"]
        stdin_reads(part, cwd, found)
        for stage in part.piped:
            stage_reads(stage, cwd, found)
        for body in part.heredocs:
            loads(marks[0].text, body, cwd, roots, found)
    return found
