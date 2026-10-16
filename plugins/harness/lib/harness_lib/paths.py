"""The state-key helpers every harness tool and hook uses: the canonical spelling of a path,
the key of a main working tree under ~/.claude/harness/repos/, and the one-time move of
state kept under an earlier, spelling-based key. See docs/design.md (repo-key).

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

import hashlib
import os
import unicodedata


def canonical_path(path):
    """The real path of `path` with each component spelled as its parent directory lists
    it, so every spelling of one directory on a case-insensitive file system gives one
    string. From the first component that cannot be found on, the rest stays as spelled."""
    real = os.path.realpath(path)
    done, parts = os.sep, [part for part in real.split(os.sep) if part]
    for i, part in enumerate(parts):
        try:
            names = os.listdir(done)
        except OSError:
            return os.path.join(done, *parts[i:])
        if part not in names:
            fold = unicodedata.normalize("NFC", part).casefold()
            given = os.path.join(done, part)
            for name in names:
                if unicodedata.normalize("NFC", name).casefold() == fold and same_entry(
                    given, os.path.join(done, name)
                ):
                    part = name
                    break
            else:
                return os.path.join(done, *parts[i:])
        done = os.path.join(done, part)
    return done


def same_entry(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def path_key(path):
    """The first 12 hex digits of the sha1 of the path string as it is spelled."""
    return hashlib.sha1(str(path).encode("utf-8", "surrogateescape")).hexdigest()[:12]


def repo_key(path):
    """The state key of a main working tree: path_key of its canonical path."""
    return path_key(canonical_path(path))


def adopt_old_state(repos, main, *spelled):
    """The state key of the canonical main tree `main`. When repos/<key> does not exist
    yet, the directory an earlier version keyed by the spelling in use is renamed to it:
    the spelling of `main` that one of `spelled` (the main tree as git reported it, the
    path the tool started from) uses. Only this repository's; when both exist, both stay."""
    key = path_key(main)
    target = os.path.join(repos, key)
    if os.path.lexists(target):
        return key
    depth = len(main.split(os.sep))
    for path in spelled:
        if not path:
            continue
        old = os.sep.join(os.path.realpath(path).split(os.sep)[:depth])
        if old == main or canonical_path(old) != main:
            continue
        source = os.path.join(repos, path_key(old))
        if os.path.isdir(source):
            try:
                os.rename(source, target)
            except OSError:
                pass
            break
    return key
