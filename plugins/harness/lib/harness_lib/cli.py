"""The command-line boilerplate every harness tool shares: one Problem, one Parser, the
`__main__` tail (run_main), warnings, JSON-or-text output, tables and issue-number arguments.
A tool's contract (docs/design.md): a failure is one line on stderr and exit 1, never a traceback.

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Callable, NoReturn

from harness_lib.text import one_line


class Problem(Exception):
    """A bad invocation, a failed precondition or a failed call: printed as one line, exit 1."""


class Parser(argparse.ArgumentParser):
    """argparse that reports a bad invocation in one line and exits 1."""

    def error(self, message):
        sys.exit(f"{self.prog}: error: {message}")


def run_main(prog: str, main: Callable[[], object]) -> NoReturn:
    """Run a tool's main(): a Problem is `<prog>: <one line>`, any other Exception
    `<prog>: <TypeName>: <one line>`, both exit 1; Ctrl-C exits 130; a reader that went away
    (`| head`) stops it quietly with exit 1. SystemExit passes through."""
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:                      # the reader went away (| head): stop quietly
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(1)
    except Problem as err:
        sys.exit(f"{prog}: {one_line(err)}")
    except Exception as err:                     # never a traceback: one line, exit 1
        sys.exit(f"{prog}: {type(err).__name__}: {one_line(err)}")
    sys.exit(0)


def warn(prog, message):
    """`<prog>: warning: <message on one line>` on stderr."""
    print(f"{prog}: warning: {one_line(message)}", file=sys.stderr)


def emit(as_json, data, text):
    """`data` as indented JSON (non-ASCII kept) when `as_json`, else `text`."""
    print(json.dumps(data, indent=2, ensure_ascii=False) if as_json else text)


def table(rows):
    """Left-aligned columns, two spaces apart."""
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    return "\n".join("  ".join([c.ljust(w) for c, w in zip(r, widths)] + [r[-1]]).rstrip() for r in rows)


def issue_number(text):
    """An argparse `type=`: a positive issue number, `#` prefixes allowed."""
    digits = text.lstrip("#")
    if not digits.isdigit() or int(digits) <= 0:
        raise argparse.ArgumentTypeError(f"not an issue number: {text!r}")
    return int(digits)
