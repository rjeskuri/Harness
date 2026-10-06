#!/bin/sh
# Install (or re-install) the harness on this machine: the marketplace, both plugins at user
# scope, and the personal CLAUDE.md. Safe to run again; each step says what it did.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
CLAUDE="${CLAUDE_BIN:-$(command -v claude 2>/dev/null || echo "$HOME/.local/bin/claude")}"
if [ ! -x "$CLAUDE" ]; then
  echo "claude CLI not found; install it (the VS Code extension's bundled binary can: '<binary> install stable') or set CLAUDE_BIN" >&2
  exit 1
fi
echo "using $CLAUDE ($("$CLAUDE" --version 2>/dev/null))"

if "$CLAUDE" plugin marketplace add "$HERE" >/tmp/harness-install.log 2>&1; then
  echo "marketplace ryan: added from $HERE"
else
  if grep -qi 'already' /tmp/harness-install.log; then
    echo "marketplace ryan: already added"
  else
    echo "marketplace ryan: add failed:"; cat /tmp/harness-install.log; exit 1
  fi
fi

for p in harness companion; do
  if "$CLAUDE" plugin install "$p@ryan" >/tmp/harness-install.log 2>&1; then
    echo "plugin $p@ryan: installed (user scope)"
  elif grep -qi 'already' /tmp/harness-install.log; then
    echo "plugin $p@ryan: already installed"
  else
    echo "plugin $p@ryan: install failed:"; cat /tmp/harness-install.log; exit 1
  fi
done

mkdir -p "$HOME/.claude"
if [ -f "$HOME/.claude/CLAUDE.md" ] && ! cmp -s "$HERE/user/CLAUDE.md" "$HOME/.claude/CLAUDE.md"; then
  cp "$HOME/.claude/CLAUDE.md" "$HOME/.claude/CLAUDE.md.bak-$(date +%Y-%m-%d)"
  echo "~/.claude/CLAUDE.md: existing file backed up"
fi
cp "$HERE/user/CLAUDE.md" "$HOME/.claude/CLAUDE.md" && echo "~/.claude/CLAUDE.md: installed"

echo
echo "Done. Start a new session (or run /reload-plugins in an open one), then:"
echo "  harness-projects discover      # find your repos"
echo "  /harness:standup               # the cross-project brief"
echo "  claude agents                  # the fleet console, in a terminal"
