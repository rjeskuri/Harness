#!/bin/sh
# Install (or re-install) the harness on this machine: the marketplace, the plugin at user
# scope, the personal CLAUDE.md, the model settings (merged into ~/.claude/settings.json) and the
# daily digest's LaunchAgent. Safe to run again; each step says what it did.
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

for p in harness; do
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

# Settings: model and effortLevel come from the snippet, modelSettings is merged per model id (the snippet's
# entries set or override theirs, other model ids stay), permissions.allow, permissions.ask,
# autoMode.environment and autoMode.allow gain each snippet entry they lack (their own entries stay, in order;
# "$defaults" leads each autoMode list exactly once), every other key stays (permissions.deny,
# permissions.defaultMode and autoMode.soft_deny included), and the retired companion plugin is dropped. A dated
# backup is kept when anything changes. A failure (an unreadable file, or a wrong-typed object or list) leaves
# the file unchanged; the remaining steps still run and the installer exits with the failure at the end.
python3 - "$HERE/user/settings.snippet.json" "$HOME/.claude/settings.json" <<'EOF'
import datetime
import json
import os
import shutil
import sys

snippet_path, target = sys.argv[1], sys.argv[2]
name = "~/.claude/settings.json"
try:
    with open(snippet_path, encoding="utf-8") as fh:
        snippet = json.load(fh)
except (OSError, ValueError) as err:
    sys.exit(f"{name}: left unchanged, cannot read {snippet_path} ({err})")
try:
    with open(target, encoding="utf-8") as fh:
        settings = json.load(fh)
    existed = True
except FileNotFoundError:
    settings, existed = {}, False
except (OSError, ValueError) as err:
    sys.exit(f"{name}: left unchanged, cannot read it ({err}); merge user/settings.snippet.json by hand")
if not isinstance(settings, dict) or not isinstance(snippet, dict):
    sys.exit(f"{name}: left unchanged, expected a JSON object in it and in {snippet_path}")
changed, models = [], []
for key in ("model", "effortLevel", "modelSettings"):
    if key not in snippet:
        continue
    if key == "modelSettings" and isinstance(snippet[key], dict):
        current = settings.get(key)
        if not isinstance(current, dict):
            current = settings[key] = {}
        for model_id, entry in snippet[key].items():
            if current.get(model_id) != entry:
                current[model_id] = entry
                models.append(model_id)
    elif settings.get(key) != snippet[key]:
        settings[key] = snippet[key]
        changed.append(key)
LISTS = (("permissions", "allow"), ("permissions", "ask"), ("autoMode", "environment"), ("autoMode", "allow"))
DEFAULTS = "$defaults"
for obj, key in LISTS:                      # checked before anything changes: a wrong type is the user's to fix
    if obj in settings and not isinstance(settings[obj], dict):
        sys.exit(f"{name}: left unchanged, {obj} is not a JSON object; merge user/settings.snippet.json by hand")
    current = settings.get(obj, {})         # an explicit null is a wrong type, only an absent key is fine
    entries = current.get(key)
    if key in current and not (isinstance(entries, list) and all(isinstance(e, str) for e in entries)):
        sys.exit(f"{name}: left unchanged, {obj}.{key} is not a list of strings; "
                 "merge user/settings.snippet.json by hand")
added, sentinel = [], []
for obj, key in LISTS:
    wanted = (snippet.get(obj) or {}).get(key)
    if not wanted:
        continue
    before = settings.get(obj, {}).get(key, [])
    lead = DEFAULTS in wanted               # the sentinel leads the list exactly once, the rest keeps its order
    after = [DEFAULTS] if lead else []
    after += [e for e in before if not (lead and e == DEFAULTS)]
    new = [e for e in wanted if e not in after]
    after += new
    if after != before:
        settings.setdefault(obj, {})[key] = after
        if new:
            added.append(f"{len(new)} {obj}.{key}")
        else:
            sentinel.append(f"{obj}.{key}")
plugins = settings.get("enabledPlugins")
dropped = isinstance(plugins, dict) and "companion@ryan" in plugins
if dropped:
    del plugins["companion@ryan"]
if not changed and not models and not added and not sentinel and not dropped:
    print(f"{name}: already up to date (model, effortLevel, modelSettings, "
          f"{', '.join(f'{obj}.{key}' for obj, key in LISTS)})")
    sys.exit(0)
backup = None
if existed:
    backup = f"{target}.bak-{datetime.date.today().isoformat()}"
    if os.path.exists(backup):              # never overwrite an earlier backup
        backup = f"{backup}-{datetime.datetime.now():%H%M%S}"
    shutil.copy2(target, backup)
with open(target, "w", encoding="utf-8") as fh:          # in place: a symlinked settings.json stays a symlink
    json.dump(settings, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
what = [f"set {', '.join(changed)}"] if changed else []
what += [f"set modelSettings for {', '.join(models)}"] if models else []
what += [f"added {', '.join(added)}"] if added else []
what += [f"put $defaults first in {', '.join(sentinel)}"] if sentinel else []
what += ['removed enabledPlugins["companion@ryan"]'] if dropped else []
print(f"{name}: {'; '.join(what)}" + (f" (backup {os.path.basename(backup)})" if backup else " (created)"))
EOF
settings_status=$?

# The daily digest at 08:30 (a LaunchAgent); a failure here never fails the install.
"$HERE/plugins/harness/bin/harness-digest" --install-launchd ||
  echo "harness-digest: the LaunchAgent is not loaded; run $HERE/plugins/harness/bin/harness-digest --install-launchd again later"

echo
if [ "$settings_status" -eq 0 ]; then
  echo "Done. Start a new session (or run /reload-plugins in an open one), then:"
else
  echo "Done, except the settings merge, which failed (see above): ~/.claude/settings.json is unchanged."
  echo "Fix it, or merge user/settings.snippet.json by hand, then run this again. Meanwhile start a new session"
  echo "(or run /reload-plugins in an open one), then:"
fi
echo "  harness-projects discover      # find your repos"
echo "  /harness:standup               # the cross-project brief"
echo "  claude agents                  # the fleet console, in a terminal"
echo
echo "Once, for /harness:work --api runs billed to the API key: store the API key in your login keychain with"
echo '  security add-generic-password -a "$USER" -s harness-anthropic -w'
echo "It prompts for the key, so the key never touches a command line or your shell history."
echo "Outside Claude Code the tools are in $HERE/plugins/harness/bin (harness-projects discover, for one)."
exit "$settings_status"
