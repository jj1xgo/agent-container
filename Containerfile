FROM docker.io/library/debian:testing-slim

ARG NODE_VERSION=latest
ARG CODEX_VERSION=latest
ARG CLAUDE_VERSION=latest
ARG AGENT_CLI_CACHEBUST=0
ARG AGENT_CONTAINER_VERSION=0.4.0-dev.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && sed -i \
      's|URIs: http://deb.debian.org|URIs: https://deb.debian.org|g' \
      /etc/apt/sources.list.d/debian.sources \
    && grep -qx 'URIs: https://deb.debian.org/debian' \
      /etc/apt/sources.list.d/debian.sources \
    && grep -qx 'URIs: https://deb.debian.org/debian-security' \
      /etc/apt/sources.list.d/debian.sources \
    && ! grep -q '^URIs: http://' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      bubblewrap ca-certificates curl git libatomic1 passwd python3 python3-pip ripgrep socat xz-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-lint.txt /opt/agent-container/

RUN python3 -m pip install --disable-pip-version-check --no-deps \
      --break-system-packages \
      -r /opt/agent-container/requirements-lint.txt

RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
      amd64) gh_arch=amd64 ;; \
      arm64) gh_arch=arm64 ;; \
      *) echo "unsupported GitHub CLI architecture" >&2; exit 1 ;; \
    esac; \
    release_url="$(curl -fsSL -o /dev/null -w '%{url_effective}' \
      https://github.com/cli/cli/releases/latest)"; \
    gh_version="$(printf '%s\n' "${release_url}" \
      | python3 -c 'import re, sys; match = re.fullmatch(r"https://github\.com/cli/cli/releases/tag/v(\d+\.\d+\.\d+)", sys.stdin.read().strip()); print(match.group(1) if match else (_ for _ in ()).throw(SystemExit("invalid GitHub CLI release URL")))')"; \
    archive="gh_${gh_version}_linux_${gh_arch}.tar.gz"; \
    checksums="gh_${gh_version}_checksums.txt"; \
    release="https://github.com/cli/cli/releases/download/v${gh_version}"; \
    curl -fsSLO "${release}/${archive}"; \
    curl -fsSLO "${release}/${checksums}"; \
    grep "  ${archive}$" "${checksums}" | sha256sum --check --strict -; \
    tar -xzf "${archive}"; \
    install -m 0755 "gh_${gh_version}_linux_${gh_arch}/bin/gh" /usr/local/bin/gh; \
    rm -rf "${archive}" "${checksums}" "gh_${gh_version}_linux_${gh_arch}"

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
    && PATH=/opt/agent-node/bin:$PATH /opt/agent-node/bin/npm install --global \
      "@openai/codex@${CODEX_VERSION}" \
      "@anthropic-ai/claude-code@${CLAUDE_VERSION}"

RUN groupadd --gid 1000 agent \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/agent agent \
    && mkdir -p /opt/agent-container /workspace \
    && chown agent:agent /workspace

COPY --chmod=0755 container/bin/codex /usr/local/bin/codex
COPY --chmod=0755 container/bin/claude /usr/local/bin/claude
COPY --chmod=0755 container/bin/git-remote-agent-broker /usr/local/bin/git-remote-agent-broker
COPY --chmod=0755 container/bin/agent-github /usr/local/bin/agent-github
COPY --chmod=0755 container/bin/agent-handover /usr/local/bin/agent-handover
COPY container/profile.d/10-agent-node.sh /etc/profile.d/10-agent-node.sh
COPY src /opt/agent-container/src
COPY profiles/codex /opt/agent-container/profiles/codex
COPY --chmod=0644 profiles/claude/managed-settings.json /etc/claude-code/managed-settings.json
COPY --chmod=0644 profiles/claude/managed-mcp.json /etc/claude-code/managed-mcp.json
COPY --chmod=0644 profiles/claude/CLAUDE.md /etc/claude-code/CLAUDE.md

ENV HOME=/home/agent \
    PATH=/usr/local/bin:/opt/agent-node/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONPATH=/opt/agent-container/src \
    AGENT_CONTAINER_VERSION=${AGENT_CONTAINER_VERSION} \
    DISABLE_UPDATES=1

USER agent
WORKDIR /workspace
CMD ["codex"]
