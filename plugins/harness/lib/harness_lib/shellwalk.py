"""The shell walker the two Bash guards, branch_guard.py and readonly_bash.py, split commands
with: `split_command` cuts a command into simple commands (on ;, &&, ||, |, & and newlines,
with marker parts around (...) subshells, and the commands inside $(...), backticks and
<(...) last, each between its own markers), `command_words` finds the command's name behind
assignments, keywords and wrappers, and `stdin_program` says whether a shell or python reads
its program from stdin. See docs/design.md (lib).

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import os
import re

MAX_DEPTH = 8  # how deep substitutions and child shells are followed

SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
WRAPPERS = {"env", "command", "builtin", "exec", "nohup", "nice", "timeout", "xargs"}
KEYWORDS = set("! { } if then elif else fi do done while until time".split())
GIT_VALUE_OPTIONS = set("-C -c --git-dir --work-tree --namespace --config-env".split())

ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
NUMBER = re.compile(r"\d+(\.\d+)?[smhd]?$")
PYTHON = re.compile(r"python[0-9.]*$")
SED_IN_PLACE = re.compile(r"-[nrsuzE]*i|--in-place(=|$)")
REDIRECT = re.compile(r"&>>|&>|>>|>\||>&|>|<<<|<<-|<<|<>|<&|<")
REDIRECTS = {
    "&>>": "out",
    "&>": "out",
    ">>": "out",
    ">|": "out",
    ">": "out",
    ">&": "dup",
    "<<<": "in",
    "<<-": "heredoc-",
    "<<": "heredoc",
    "<>": "out",
    "<&": "in",
    "<": "in",
}


class Part:
    """One simple command: its words without quotes, where its output goes, the bodies
    of its heredocs, its text as written and the operator after it (;, &&, ||, |, &, or
    "" at a newline or the end). A marker part, with no words, opens ("(") or closes
    (")") a subshell."""

    def __init__(self, marker: str = "") -> None:
        self.words: list[str] = []
        self.targets: list[str] = []
        self.heredocs: list[str] = []
        self.text = ""
        self.sep = ""
        self.marker = marker


def closing(text: str, i: int) -> int:
    """Index of the ")" that closes the "(" at text[i], or len(text); quotes skipped."""
    depth, quote, j = 0, "", i
    while j < len(text):
        ch = text[j]
        if quote:
            if ch == "\\" and quote == '"':
                j += 1
            elif ch == quote:
                quote = ""
        elif ch == "\\":
            j += 1
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return len(text)


def substitution(text: str, i: int, nested: list[str]) -> int:
    """Keeps the command inside the $(...) or `...` at text[i]; returns the index after it.
    Arithmetic, $((...)), holds no command."""
    if text.startswith("$((", i):
        return closing(text, i + 1) + 1
    if text[i] == "`":
        j = text.find("`", i + 1)
        j = len(text) if j < 0 else j
        nested.append(text[i + 1 : j])
        return j + 1
    j = closing(text, i + 1)
    nested.append(text[i + 2 : j])
    return j + 1


def split_command(command: str, depth: int = 0) -> list[Part]:
    """The simple commands in `command`, with marker parts around subshells; those inside
    substitutions come last, each in its own subshell."""
    parts: list[Part] = []
    nested: list[str] = []
    heredocs: list[tuple[str, bool, Part]] = []
    part, start = Part(), 0
    word: str | None = None
    redirect = ""  # what the next word is: "out", "dup", "in", "heredoc" or "heredoc-"
    i, n = 0, len(command)

    def end_word() -> None:
        nonlocal word, redirect
        if word is None:
            return
        if redirect == "out" or (
            redirect == "dup" and not (word.isdigit() or word == "-")
        ):
            part.targets.append(word)
        elif redirect.startswith("heredoc"):
            heredocs.append((word, redirect == "heredoc-", part))
        elif not redirect:
            part.words.append(word)
        word, redirect = None, ""

    def end_part(end: int, op: str = "") -> None:
        nonlocal part, start, redirect
        end_word()
        if redirect in ("out", "dup"):
            part.targets.append("")  # a redirection with no target
        redirect = ""
        part.text = command[start:end].strip()
        part.sep = op
        if part.words or part.targets:
            parts.append(part)
        part, start = Part(), end + max(len(op), 1)

    while i < n:
        c = command[i]
        if c in " \t\r":
            end_word()
            i += 1
        elif c == "\n":
            end_part(i)
            i += 1
            for delimiter, tabs, owner in heredocs:  # their bodies follow this line
                body = []
                while i < n:
                    j = command.find("\n", i)
                    j = n if j < 0 else j
                    line, i = command[i:j], j + 1
                    if (line.lstrip("\t") if tabs else line) == delimiter:
                        break
                    body.append(line)
                owner.heredocs.append("\n".join(body))
            heredocs.clear()
            start = i
        elif c == "#" and word is None:
            j = command.find("\n", i)
            i = n if j < 0 else j
        elif c == "\\":
            if command[i + 1 : i + 2] != "\n":
                word = (word or "") + command[i + 1 : i + 2]
            i += 2
        elif c == "'":
            j = command.find("'", i + 1)
            j = n if j < 0 else j
            word = (word or "") + command[i + 1 : j]
            i = j + 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and command[j] != '"':
                if command[j] == "\\" and j + 1 < n:
                    if command[j + 1] in '"\\$`':
                        buf.append(command[j + 1])
                    elif command[j + 1] != "\n":
                        buf.append(command[j : j + 2])
                    j += 2
                elif command[j] == "`" or command.startswith("$(", j):
                    k = substitution(command, j, nested)
                    buf.append(command[j:k])
                    j = k
                else:
                    buf.append(command[j])
                    j += 1
            word = (word or "") + "".join(buf)
            i = j + 1
        elif c == "`" or command.startswith("$(", i):
            k = substitution(command, i, nested)
            word = (word or "") + command[i:k]
            i = k
        elif command.startswith("((", i) and word is None:  # arithmetic, no command
            i = closing(command, i) + 1
        elif c in "<>" and command.startswith("(", i + 1):  # process substitution
            end_word()
            k = closing(command, i + 1)
            nested.append(command[i + 2 : k])
            i = k + 1
        elif c in "<>" or command.startswith("&>", i):
            if word is not None and word.isdigit() and c != "&":
                word = None  # a file descriptor number, as in 2>
            end_word()
            op = REDIRECT.match(command, i).group(0)
            redirect = REDIRECTS[op]
            i += len(op)
        elif c in ";|&()":
            op = command[i : i + 2] if command.startswith(("&&", "||"), i) else c
            end_part(i, op)
            i += len(op)
            if c in "()":
                parts.append(Part(marker=c))
        else:
            word = (word or "") + c
            i += 1
    end_part(n)
    if depth < MAX_DEPTH:
        for inner in nested:
            parts.append(Part(marker="("))
            parts.extend(split_command(inner, depth + 1))
            parts.append(Part(marker=")"))
    return parts


def command_words(words: list[str]) -> list[str]:
    """`words` from the command's name on: assignments, keywords and wrappers skipped."""
    k = 0
    while k < len(words):
        w = words[k]
        if ASSIGNMENT.match(w) or w in KEYWORDS:
            k += 1
        elif os.path.basename(w) in WRAPPERS:
            k += 1
            while k < len(words) and (
                words[k].startswith("-")
                or ASSIGNMENT.match(words[k])
                or NUMBER.match(words[k])
            ):
                k += 1
        else:
            break
    return words[k:]


def stdin_program(args: list[str]) -> bool:
    """True when a shell or python given `args` reads its program from stdin."""
    for a in args:
        if a == "-":
            return True
        if not a.startswith("-") or a.startswith(("-c", "-m")):
            return False
    return True
