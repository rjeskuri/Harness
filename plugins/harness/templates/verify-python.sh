#!/bin/sh
# The whole suite plus lint for {{name}}: CLAUDE_VERIFY_CMD, the stop hook and CI run this.
# Each check on its own line, so `set -e` stops at the first failure.
set -eu
cd "$(dirname "$0")/.."

uv run ruff check .
uv run ruff format --check .
uv run pytest -q

echo VERIFY OK
