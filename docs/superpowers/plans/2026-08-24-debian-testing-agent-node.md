# Debian Testing and Agent Node Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the common agent image from Debian testing slim with a checksum-verified official Node.js runtime that is independent from future project Node versions.

**Architecture:** Replace the official Node base image with `debian:testing-slim`, install the agent runtime under `/opt/agent-node`, and launch both CLIs through fixed wrappers in `/usr/local/bin`. Keep latest versions as build defaults while retaining explicit version overrides for reproducibility and rollback.

**Tech Stack:** Containerfile, Debian APT, nodejs.org binary archives, Python `unittest`, rootless Podman

**Spec:** `docs/superpowers/specs/2026-08-24-debian-project-images-claude-sandbox-design.md`

## Global Constraints

- Base image is exactly `docker.io/library/debian:testing-slim`.
- Node.js is downloaded only from `https://nodejs.org/dist/` and checked against that release's `SHASUMS256.txt`.
- Default Codex, Claude Code, and agent Node versions are `latest`; explicit versions remain supported for rollback.
- Agent Node lives at `/opt/agent-node`; agent wrappers must not resolve Node through runtime `PATH`.
- Runtime remains user `agent`, working directory `/workspace`, and contains no credentials.
- Existing dirty changes in `docs/codex-operations.md`, `docs/phase2-claude-code.md`, and `docs/phase2-smoke-test.md` belong to the user and must be preserved and reconciled, never overwritten.

## File Structure

- Modify `Containerfile`: build Debian testing base, install official Node, install CLIs, and copy wrappers.
- Create `container/bin/codex`: fixed agent-Node launcher for Codex.
- Create `container/bin/claude`: fixed agent-Node launcher for Claude Code.
- Modify `.containerignore`: allow only the new tracked wrapper directory in addition to existing build inputs.
- Modify `src/agent_container/agentctl.py`: expose and validate `--node-version`.
- Modify `src/agent_container/podman.py`: pass `NODE_VERSION` to Podman build.
- Modify `tests/container/test_image.py`: assert the new image and wrapper contracts.
- Modify `tests/container/test_podman.py`: assert the new build argument.
- Modify `tests/container/test_agentctl.py`: assert parser, validation, and version reporting.
- Modify `docs/phase2-claude-code.md`: document latest-by-default rebuild and rollback pins.

---

### Task 1: Add the Agent Node Build Argument

**Files:**
- Modify: `src/agent_container/agentctl.py:90-97, 756-790`
- Modify: `src/agent_container/podman.py:67-92`
- Test: `tests/container/test_agentctl.py`
- Test: `tests/container/test_podman.py`

**Interfaces:**
- Consumes: existing `validate_version(value: str)` and the current common-image build flow.
- Produces: `build_image_spec(repo_root, image, node_version, codex_version, claude_version, cachebuster) -> CommandSpec` and parser field `arguments.node_version`.

- [ ] **Step 1: Write failing parser and Podman command tests**

```python
# tests/container/test_agentctl.py, in AgentCtlParserTest
build = parser().parse_args(["build"])
self.assertEqual(
    (build.node_version, build.codex_version, build.claude_version),
    ("latest", "latest", "latest"),
)

# tests/container/test_podman.py
spec = build_image_spec(
    Path("/repo"), IMAGE, "22.23.1", "0.149.0", "1.2.3", "12345"
)
self.assertIn("NODE_VERSION=22.23.1", spec.argv)
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `python3 -m unittest tests.container.test_podman.PodmanCommandTest.test_build_uses_versions_cachebuster_and_repository_context tests.container.test_agentctl.AgentCtlParserTest.test_new_command_contract -v`

Expected: FAIL because `build_image_spec` has no Node argument and the parser has no `node_version`.

- [ ] **Step 3: Implement the argument and validation**

```python
# src/agent_container/agentctl.py
build.add_argument("--node-version", default="latest")

if arguments.command == "build":
    validate_version(arguments.node_version)
    validate_version(arguments.codex_version)
    validate_version(arguments.claude_version)

# src/agent_container/podman.py
def build_image_spec(
    repo_root: Path,
    image: str,
    node_version: str,
    codex_version: str,
    claude_version: str,
    cachebuster: str,
) -> CommandSpec:
    root = repo_root.resolve()
    return CommandSpec(
        (
            "podman", "build",
            "--build-arg", f"NODE_VERSION={node_version}",
            "--build-arg", f"CODEX_VERSION={codex_version}",
            "--build-arg", f"CLAUDE_VERSION={claude_version}",
            "--build-arg", f"AGENT_CLI_CACHEBUST={cachebuster}",
            "--tag", image,
            "--file", str(root / "Containerfile"),
            str(root),
        ),
        {},
    )
```

Update the call in `main()` and every existing test caller in the same change. Preserve argument order as Node, Codex, Claude, cachebuster.

- [ ] **Step 4: Add an invalid Node version test**

```python
result = main(
    ["build", "--node-version", "../bad"],
    runner=lambda spec: calls.append(spec),
    stderr=stderr,
)
self.assertEqual(result, 1)
self.assertEqual(calls, [])
self.assertIn("version", stderr.getvalue())
```

- [ ] **Step 5: Run focused tests**

Run: `python3 -m unittest tests.container.test_podman tests.container.test_agentctl.AgentCtlParserTest -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/agent_container/agentctl.py src/agent_container/podman.py tests/container/test_agentctl.py tests/container/test_podman.py
git commit -m "feat: add agent Node build version"
```

### Task 2: Install Official Node on Debian Testing

**Files:**
- Modify: `Containerfile`
- Create: `container/bin/codex`
- Create: `container/bin/claude`
- Modify: `.containerignore`
- Test: `tests/container/test_image.py`

**Interfaces:**
- Consumes: `NODE_VERSION`, `CODEX_VERSION`, `CLAUDE_VERSION`, and `AGENT_CLI_CACHEBUST` build args.
- Produces: `/opt/agent-node/bin/node`, `/usr/local/bin/codex`, `/usr/local/bin/claude`, and non-root user `agent` with UID/GID 1000.

- [ ] **Step 1: Replace old image-contract tests with failing Debian and Node tests**

```python
body = (ROOT / "Containerfile").read_text(encoding="utf-8")
self.assertIn("FROM docker.io/library/debian:testing-slim", body)
self.assertIn("ARG NODE_VERSION=latest", body)
self.assertIn("https://nodejs.org/dist/", body)
self.assertIn("SHASUMS256.txt", body)
self.assertIn("/opt/agent-node", body)
self.assertNotIn("FROM docker.io/library/node:", body)

for wrapper in ("codex", "claude"):
    wrapper_body = (ROOT / f"container/bin/{wrapper}").read_text(encoding="utf-8")
    self.assertIn("exec /opt/agent-node/bin/node", wrapper_body)
    self.assertNotIn("/usr/bin/env node", wrapper_body)
```

Also replace `test_image_reuses_base_node_identity_for_agent` with assertions for explicit UID/GID 1000 creation because Debian no longer provides the `node` user.

- [ ] **Step 2: Run the image-contract tests and confirm failure**

Run: `python3 -m unittest tests.container.test_image -v`

Expected: FAIL on the base image, Node source, wrapper files, and user creation contract.

- [ ] **Step 3: Add fixed CLI wrappers**

```sh
# container/bin/codex
#!/bin/sh
set -eu
exec /opt/agent-node/bin/node /opt/agent-node/bin/codex "$@"
```

```sh
# container/bin/claude
#!/bin/sh
set -eu
exec /opt/agent-node/bin/node /opt/agent-node/bin/claude "$@"
```

The wrapper path differs from the npm-created CLI symlink, so Node can follow `/opt/agent-node/bin/<agent>` without recursion.

- [ ] **Step 4: Implement the Debian testing Containerfile**

Use this structure, retaining the existing common package set:

```dockerfile
FROM docker.io/library/debian:testing-slim

ARG NODE_VERSION=latest
ARG CODEX_VERSION=latest
ARG CLAUDE_VERSION=latest
ARG AGENT_CLI_CACHEBUST=0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      bubblewrap ca-certificates curl gh git python3 socat xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
      amd64) node_arch=x64 ;; \
      arm64) node_arch=arm64 ;; \
      *) echo "unsupported Node architecture" >&2; exit 1 ;; \
    esac; \
    if [ "$NODE_VERSION" = latest ]; then \
      resolved="$(curl -fsSL https://nodejs.org/dist/index.json \
        | python3 -c 'import json,sys; print(next(r["version"][1:] for r in json.load(sys.stdin) if "-" not in r["version"]))')"; \
    else resolved="$NODE_VERSION"; fi; \
    archive="node-v${resolved}-linux-${node_arch}.tar.xz"; \
    curl -fsSLO "https://nodejs.org/dist/v${resolved}/${archive}"; \
    curl -fsSLO "https://nodejs.org/dist/v${resolved}/SHASUMS256.txt"; \
    grep "  ${archive}$" SHASUMS256.txt | sha256sum --check --strict -; \
    mkdir -p /opt/agent-node; \
    tar -xJf "$archive" -C /opt/agent-node --strip-components=1; \
    rm -f "$archive" SHASUMS256.txt

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
```

Keep the existing source/profile copies, environment, `USER agent`, and `WORKDIR /workspace`. Set base `PATH=/usr/local/bin:/opt/agent-node/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin`; the later derived-image plan will prepend project Node only for project commands.

- [ ] **Step 5: Update `.containerignore` and its exact allowlist test**

Add only:

```text
!container/
!container/bin/
!container/bin/codex
!container/bin/claude
```

Extend the tracked-copy-input test to include `container/bin`. Keep all existing credential and state exclusions.

- [ ] **Step 6: Run image-contract tests**

Run: `python3 -m unittest tests.container.test_image -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add Containerfile container/bin/codex container/bin/claude .containerignore tests/container/test_image.py
git commit -m "feat: build agents on official Node"
```

### Task 3: Build and Verify the Common Image

**Files:**
- Modify: `src/agent_container/podman.py`
- Modify: `tests/container/test_agentctl.py`
- Modify: `tests/container/test_podman.py`
- Modify: `docs/phase2-claude-code.md`

**Interfaces:**
- Consumes: `bin/agentctl build [--node-version V] [--codex-version V] [--claude-version V]`.
- Produces: a verified `localhost/agent-container:dev` with three reported versions.

- [ ] **Step 1: Add a failing controller test for Node version probing**

Add `node_version_spec(image: str) -> CommandSpec` in the expected call sequence and assert build output contains only labels and tool versions:

```python
self.assertIn("Node version: v", stdout.getvalue())
self.assertIn("Codex version:", stdout.getvalue())
self.assertIn("Claude version:", stdout.getvalue())
```

- [ ] **Step 2: Run the focused controller test and confirm failure**

Run: `python3 -m unittest tests.container.test_agentctl.AgentCtlBuildAuthTest -v`

Expected: FAIL because the controller reports only Codex and Claude.

- [ ] **Step 3: Implement hardened Node probing**

Add `node_version_spec(image)` beside `cli_version_spec`, using the same read-only, cap-drop, no-new-privileges, keep-id, mount-free prefix and command `node --version`. In `main()`, probe Node before the two agent CLIs and print `Node version: <value>`.

- [ ] **Step 4: Run controller and Podman tests**

Run: `python3 -m unittest tests.container.test_agentctl tests.container.test_podman -v`

Expected: PASS.

- [ ] **Step 5: Reconcile and update operator documentation**

In `docs/phase2-claude-code.md`, preserve the user's existing dirty edits and add:

```text
通常の agentctl build は agent Node、Codex、Claude Code の最新安定版を取得する。
障害再現またはrollback時だけ --node-version、--codex-version、--claude-version を指定する。
```

- [ ] **Step 6: Run documentation and full unit tests**

Run: `python3 -m unittest tests.container.test_docs -v`

Run: `python3 -m unittest discover -s tests -v`

Expected: PASS with no unexpected test count reduction.

- [ ] **Step 7: Build the real image with latest versions**

Run: `bin/agentctl build`

Expected: exit 0; output reports Node, Codex, and Claude versions. Do not record tokens, environment listings, or credential file contents.

- [ ] **Step 8: Verify runtime identity and wrappers**

Run: `podman run --rm --read-only --cap-drop=all --security-opt=no-new-privileges --userns=keep-id:uid=1000,gid=1000 localhost/agent-container:dev sh -lc 'id -u; node --version; codex --version; claude --version'`

Expected: UID `1000`; all three version commands exit 0.

- [ ] **Step 9: Commit**

```bash
git add src/agent_container/agentctl.py src/agent_container/podman.py tests/container/test_agentctl.py tests/container/test_podman.py docs/phase2-claude-code.md
git commit -m "test: verify Debian agent image"
```
