FROM docker.io/library/debian:testing-slim

ARG NODE_VERSION=latest
ARG CODEX_VERSION=latest
ARG CLAUDE_VERSION=latest
ARG AGENT_CLI_CACHEBUST=0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      bubblewrap ca-certificates curl gh git passwd python3 socat xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
      amd64) node_arch=x64 ;; \
      arm64) node_arch=arm64 ;; \
      *) echo "unsupported Node architecture" >&2; exit 1 ;; \
    esac; \
    if [ "${NODE_VERSION}" = latest ]; then \
      resolved="$(curl -fsSL https://nodejs.org/dist/index.json \
        | python3 -c 'import json, sys; print(next(release["version"][1:] for release in json.load(sys.stdin) if "-" not in release["version"]))')"; \
    else \
      resolved="${NODE_VERSION}"; \
    fi; \
    archive="node-v${resolved}-linux-${node_arch}.tar.xz"; \
    curl -fsSLO "https://nodejs.org/dist/v${resolved}/${archive}"; \
    curl -fsSLO "https://nodejs.org/dist/v${resolved}/SHASUMS256.txt"; \
    grep "  ${archive}$" SHASUMS256.txt | sha256sum --check --strict -; \
    mkdir -p /opt/agent-node; \
    tar -xJf "${archive}" -C /opt/agent-node --strip-components=1; \
    rm -f "${archive}" SHASUMS256.txt

RUN test -n "${AGENT_CLI_CACHEBUST}" \
    && /opt/agent-node/bin/npm install --global \
      "@openai/codex@${CODEX_VERSION}" \
      "@anthropic-ai/claude-code@${CLAUDE_VERSION}"

RUN groupadd --gid 1000 agent \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/agent agent \
    && mkdir -p /opt/agent-container /workspace \
    && chown agent:agent /workspace

COPY --chmod=0755 container/bin/codex /usr/local/bin/codex
COPY --chmod=0755 container/bin/claude /usr/local/bin/claude
COPY src /opt/agent-container/src
COPY profiles/codex /opt/agent-container/profiles/codex

ENV HOME=/home/agent \
    PATH=/usr/local/bin:/opt/agent-node/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONPATH=/opt/agent-container/src \
    GH_CONFIG_DIR=/home/agent/.config/gh \
    DISABLE_UPDATES=1

USER agent
WORKDIR /workspace
CMD ["codex"]
