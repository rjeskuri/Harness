#!/bin/sh
# The whole suite plus lint for Harness: CLAUDE_VERIFY_CMD, the stop hook and CI run this.
# Each check on its own line, so `set -e` stops at the first failure.
set -eu
cd "$(dirname "$0")/.."

python3 scripts/run_suite.py

echo VERIFY OK
