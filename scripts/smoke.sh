#!/bin/sh
# The Harness canary: compile the plugin, run each hook on fixture events, run every tool's --help.
# Exit 0 all well, 1 a big failure, 2 only a reporting tool broken. See scripts/canary.py.
exec python3 "$(dirname "$0")/canary.py" "$@"
