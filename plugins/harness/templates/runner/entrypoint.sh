#!/bin/bash
# harness-runner's entrypoint (docs/design.md, harness-runner): configure once, then run.
# config.sh writes the registration (.runner, .credentials, .credentials_rsaparams, .env, .path) into
# /home/runner beside the runner's own binaries, so the volume cannot be mounted there: an image upgrade would
# keep the old binaries. The repository's named volume is mounted at /home/runner/config instead; the first
# start registers with RUNNER_TOKEN (RUNNER_URL, RUNNER_NAME; from harness-runner add's env file) and saves the
# files there, and every later start, a re-created container's included, copies them back and needs no token.
set -euo pipefail
cd /home/runner
conf=/home/runner/config
files=(.runner .credentials .credentials_rsaparams .env .path)
if [ -f "$conf/.runner" ]; then
  for f in "${files[@]}"; do
    if [ -f "$conf/$f" ]; then cp "$conf/$f" "./$f"; fi
  done
else
  : "${RUNNER_TOKEN:?no registration token and no registration saved in /home/runner/config}"
  ./config.sh --unattended --url "$RUNNER_URL" --token "$RUNNER_TOKEN" --name "$RUNNER_NAME" \
    --labels harness-mac --work _work --replace
  for f in "${files[@]}"; do
    if [ -f "$f" ]; then cp "$f" "$conf/$f" && chmod 600 "$conf/$f"; fi
  done
fi
unset RUNNER_TOKEN
exec ./run.sh
