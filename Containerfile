FROM docker.io/library/node:22-bookworm-slim

ARG CODEX_VERSION=0.149.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap ca-certificates gh git python3 \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global "@openai/codex@${CODEX_VERSION}"

RUN groupmod --new-name agent node \
    && usermod --login agent --home /home/agent --move-home node \
    && mkdir -p /opt/agent-container /workspace \
    && chown agent:agent /workspace

COPY src /opt/agent-container/src
COPY profiles/codex /opt/agent-container/profiles/codex

ENV HOME=/home/agent \
    PYTHONPATH=/opt/agent-container/src \
    GH_CONFIG_DIR=/home/agent/.config/gh

USER agent
WORKDIR /workspace
CMD ["codex"]
