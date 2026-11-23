# harness-runner's image (docs/design.md, harness-runner): GitHub's runner image, the version pinned, plus the
# build tools the projects' CI uses. harness-runner tags it harness-runner:<the version in the FROM line>.
FROM ghcr.io/actions/actions-runner:2.338.2
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends make build-essential git jq curl unzip zip ca-certificates \
      python3 python3-venv python3-pip xz-utils \
 && install -d -m 755 /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*
# The mount point of the repository's named volume, owned by runner: an empty named volume takes the owner and
# mode of the image's directory on first use, so the runner user can write its registration there.
RUN install -d -o runner -g runner -m 700 /home/runner/config
COPY entrypoint.sh /home/runner/entrypoint.sh
RUN chmod 755 /home/runner/entrypoint.sh
USER runner
ENTRYPOINT ["/home/runner/entrypoint.sh"]
