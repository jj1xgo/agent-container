# Phase 2 Claude Code Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Claude Code as a selectable, isolated runtime with shared authentication, project-local state, safe legacy configuration migration, and no Codex regression.

**Architecture:** Keep one rootless Podman image and select `codex` or `claude` at launch. Reuse the workspace, GitHub, handover, and Podman boundaries while giving Claude a shared `.credentials.json` and a separate per-project `CLAUDE_CONFIG_DIR`; implement migration as a host-side, dry-run-first, allowlisted atomic copy.

**Tech Stack:** Python 3 standard library, `unittest`, rootless Podman, Debian/Node container image, Codex CLI, Claude Code CLI, Git/GitHub CLI.

**Spec:** `docs/superpowers/specs/2026-08-23-phase-2-claude-code-design.md`

## Global Constraints

- Retain rootless Podman, `--read-only`, `--cap-drop=all`, `no-new-privileges`, keep-id, and the bounded `/tmp` tmpfs.
- Never mount host `~/.codex`, host `~/.claude`, old `claude-container`, another workspace/project state, or the Podman socket.
- Directories are `0700`; credentials and metadata are `0600`; reject symlinks and paths escaping configured roots.
- Share only `shared-auth/claude/.credentials.json`; Claude configuration, sessions, plugins, memory, and cache stay project-scoped.
- `run` and `doctor` default to `codex`; existing commands remain compatible.
- Normal build fetches `latest` for both CLIs with an invalidated CLI-install layer; explicit versions support rollback; runtime updates stay disabled.
- Migration is explicit-source, dry-run by default, no-overwrite, atomic, and excludes credentials, histories, transcripts, handovers, cache, logs, and Git metadata.
- Never print credential bodies, token/device codes, environment values, or secret fixture content.
- Keep a Phase 2 network-policy WARN; domain allowlisting is out of scope.
- Use TDD and end each task with its own focused commit.

---

### Task 1: Agent CLI contract and Claude state paths

**Files:**
- Modify: `src/agent_container/state.py`
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_state.py`
- Modify: `tests/container/test_agentctl.py`

**Interfaces:**
- Produces: `validate_agent(value: str, allow_all: bool = False) -> str`
- Produces: `validate_version(value: str) -> str`
- Produces: `validate_plugin_identifier(value: str) -> str`
- Produces: `StateLayout.claude_auth_dir`, `.claude_auth_file`, and `.claude_config`
- Produces parsed build, auth, run, doctor, and migrate arguments.

- [ ] **Step 1: Write failing state and validation tests**

Add imports and these cases to `tests/container/test_state.py`:

```python
from agent_container.state import validate_agent
from agent_container.state import validate_plugin_identifier
from agent_container.state import validate_version

def test_state_layout_has_claude_paths(self) -> None:
    with TemporaryDirectory() as temp:
        root = Path(temp).resolve()
        layout = StateLayout.from_environment(
            "agent-container", {"AGENT_CONTAINER_HOME": temp}
        )
        self.assertEqual(layout.claude_auth_dir, root / "shared-auth/claude")
        self.assertEqual(
            layout.claude_auth_file,
            root / "shared-auth/claude/.credentials.json",
        )
        self.assertEqual(
            layout.claude_config,
            root / "projects/agent-container/claude-config",
        )

def test_agent_version_and_plugin_validation(self) -> None:
    self.assertEqual(validate_agent("claude"), "claude")
    self.assertEqual(validate_agent("all", allow_all=True), "all")
    self.assertEqual(validate_version("latest"), "latest")
    self.assertEqual(validate_version("2.1.89"), "2.1.89")
    self.assertEqual(
        validate_plugin_identifier("issue-ops@local-marketplace"),
        "issue-ops@local-marketplace",
    )
    for value in ("", "all", "../claude", "claude\nnext"):
        with self.subTest(agent=value), self.assertRaises(ValueError):
            validate_agent(value)
    for value in ("", "-latest", "two words", "2.1.89\nnext"):
        with self.subTest(version=value), self.assertRaises(ValueError):
            validate_version(value)
    for value in ("plugin", "../p@m", "p@../m", "p/x@m"):
        with self.subTest(plugin=value), self.assertRaises(ValueError):
            validate_plugin_identifier(value)
```

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_state -v`

Expected: import errors and missing Claude properties.

- [ ] **Step 3: Implement state paths and validators**

Add to `state.py`:

```python
AGENTS = frozenset({"codex", "claude"})
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,99}$")
PLUGIN_IDENTIFIER = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}@[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
)

def validate_agent(value: str, allow_all: bool = False) -> str:
    allowed = AGENTS | ({"all"} if allow_all else set())
    if value not in allowed:
        raise ValueError("agent must be codex or claude")
    return value

def validate_version(value: str) -> str:
    if VERSION.fullmatch(value) is None:
        raise ValueError("version must be a safe npm version or latest")
    return value

def validate_plugin_identifier(value: str) -> str:
    if PLUGIN_IDENTIFIER.fullmatch(value) is None:
        raise ValueError("plugin must be NAME@MARKETPLACE")
    return value
```

Add `StateLayout` properties returning `shared-auth/claude`, its `.credentials.json`, and `project_dir / "claude-config"`.

- [ ] **Step 4: Write failing parser tests**

Import `parser` in `test_agentctl.py`, then assert:

```python
class AgentCtlParserTest(unittest.TestCase):
    def test_new_command_contract(self) -> None:
        build = parser().parse_args(["build"])
        self.assertEqual((build.codex_version, build.claude_version), ("latest", "latest"))
        self.assertEqual(parser().parse_args(["run", "p"]).agent, "codex")
        self.assertEqual(
            parser().parse_args(["run", "p", "--agent", "claude"]).agent,
            "claude",
        )
        self.assertEqual(
            parser().parse_args(["doctor", "p", "--agent", "all"]).agent,
            "all",
        )
        migrate = parser().parse_args(
            ["migrate", "claude", "p", "--from", "/old/.claude",
             "--plugin", "issue-ops@local-marketplace"]
        )
        self.assertEqual(migrate.source, Path("/old/.claude"))
        self.assertEqual(migrate.plugins, ["issue-ops@local-marketplace"])
        self.assertFalse(migrate.apply)
```

- [ ] **Step 5: Verify RED, implement parser, and validate before effects**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlParserTest -v`

Expected: FAIL for missing arguments/commands.

Implement exact forms:

```text
build [--codex-version VERSION] [--claude-version VERSION]
auth {codex,claude}
run PROJECT [--agent {codex,claude}]
doctor PROJECT [--agent {codex,claude,all}]
migrate claude PROJECT --from PATH [--plugin ID]... [--apply]
```

Call the validators immediately after parsing and before runner calls or file creation.

- [ ] **Step 6: Verify GREEN and regression**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_state tests.container.test_agentctl.AgentCtlParserTest -v`

Expected: PASS.

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/agent_container/state.py src/agent_container/agentctl.py \
  tests/container/test_state.py tests/container/test_agentctl.py
git commit -m "feat: define Claude agent CLI and state paths"
```

---

### Task 2: Latest-at-build installation and version probes

**Files:**
- Modify: `Containerfile`
- Modify: `src/agent_container/podman.py`
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_image.py`
- Modify: `tests/container/test_podman.py`
- Modify: `tests/container/test_agentctl.py`

**Interfaces:**
- Produces: `build_image_spec(repo_root, image, codex_version, claude_version, cachebuster) -> CommandSpec`
- Produces: `cli_version_spec(image: str, agent: str) -> CommandSpec`
- Produces: `read_cachebuster() -> str`

- [ ] **Step 1: Write failing image and command-spec tests**

Require these Containerfile strings in `test_image.py`:

```python
self.assertIn("ARG CODEX_VERSION=latest", body)
self.assertIn("ARG CLAUDE_VERSION=latest", body)
self.assertIn("ARG AGENT_CLI_CACHEBUST=0", body)
self.assertIn("@openai/codex@${CODEX_VERSION}", body)
self.assertIn("@anthropic-ai/claude-code@${CLAUDE_VERSION}", body)
self.assertIn("DISABLE_UPDATES=1", body)
```

In `test_podman.py`, assert the build argv includes all three build args and that each `cli_version_spec(IMAGE, agent)` ends with `(agent, "--version")`, has no mounts, and retains read-only/cap-drop/no-new-privileges.

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_image tests.container.test_podman -v`

Expected: Claude/image assertions and new signatures FAIL.

- [ ] **Step 3: Implement the image and Podman specs**

Use:

```dockerfile
ARG CODEX_VERSION=latest
ARG CLAUDE_VERSION=latest
ARG AGENT_CLI_CACHEBUST=0
RUN test -n "${AGENT_CLI_CACHEBUST}" \
    && npm install --global \
      "@openai/codex@${CODEX_VERSION}" \
      "@anthropic-ai/claude-code@${CLAUDE_VERSION}"
ENV DISABLE_UPDATES=1
```

`build_image_spec()` must pass `--build-arg CODEX_VERSION=...`, `CLAUDE_VERSION=...`, and `AGENT_CLI_CACHEBUST=...` before the existing tag/file/context arguments. `cli_version_spec()` must use `podman run --rm --read-only --cap-drop=all --security-opt=no-new-privileges IMAGE AGENT --version` and no mount.

- [ ] **Step 4: Write failing orchestration tests**

Inject `cachebuster_reader=lambda: "12345"`; assert the build argv contains requested versions and `12345`, then that Codex and Claude probes run in order and their stdout is printed only as version lines. Add a Claude probe exit `23` test that asserts `main()` returns `23` and does not print probe stderr.

- [ ] **Step 5: Verify RED and implement orchestration**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlBuildAuthTest -v`

Expected: FAIL for old build signature and missing probes.

Define `read_cachebuster()` as `str(time.time_ns())`, inject it into `main()`, run build, then `_required_probe_run()` for both agents. Print `Codex version: <stdout>` and `Claude version: <stdout>` only.

- [ ] **Step 6: Verify GREEN and commit**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Expected: all tests PASS.

```bash
git add Containerfile src/agent_container/podman.py src/agent_container/agentctl.py \
  tests/container/test_image.py tests/container/test_podman.py tests/container/test_agentctl.py
git commit -m "feat: install and verify current agent CLIs"
```

---

### Task 3: Claude authentication adapter

**Files:**
- Modify: `src/agent_container/podman.py`
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_podman.py`
- Modify: `tests/container/test_agentctl.py`

**Interfaces:**
- Produces: `auth_claude_spec(layout, image) -> CommandSpec`
- Produces: `claude_login_status_spec(layout, image) -> CommandSpec`
- Produces: `_prepare_claude_auth(layout: StateLayout) -> None`

- [ ] **Step 1: Write failing auth spec tests**

For login and status, assert:

```python
joined = " ".join(spec.argv)
self.assertIn("src=/state/shared-auth/claude,dst=/home/agent/.claude", joined)
self.assertIn("CLAUDE_CONFIG_DIR=/home/agent/.claude", joined)
self.assertNotIn("/workspace", joined)
self.assertNotIn("token", joined.lower())
```

Login must end `claude auth login`; status must end `claude auth status`.

- [ ] **Step 2: Verify RED and implement command specs**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_podman -v`

Expected: missing Claude functions.

Create a shared `_claude_auth_prefix()` using `_runtime_prefix()`, one rw mount at `/home/agent/.claude`, and the `CLAUDE_CONFIG_DIR` environment argument. Append only image plus the relevant Claude command.

- [ ] **Step 3: Write failing orchestration and safety tests**

Mirror Codex auth tests with `.credentials.json`. During fake login create a mode-`0600` marker file. Assert status runs, directories are `0700`, missing image creates no state, mode `0644` and symlinks fail before status, exit `17` propagates, and `DO-NOT-PRINT-CLAUDE-CREDENTIAL` never appears in output.

- [ ] **Step 4: Verify RED and implement Claude auth dispatch**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlBuildAuthTest -v`

Expected: Claude cases FAIL.

Implement `_validate_existing_claude_auth()` and `_prepare_claude_auth()` with existing private helpers. Preserve order: validate existing paths, Podman/image preflight, prepare directories, login, validate credential file, status, require success. Never read the credential body.

- [ ] **Step 5: Verify GREEN and commit**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Expected: all tests PASS.

```bash
git add src/agent_container/podman.py src/agent_container/agentctl.py \
  tests/container/test_podman.py tests/container/test_agentctl.py
git commit -m "feat: add isolated Claude authentication"
```

---

### Task 4: Claude runtime and lazy project state

**Files:**
- Modify: `src/agent_container/podman.py`
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_podman.py`
- Modify: `tests/container/test_agentctl.py`

**Interfaces:**
- Produces: `run_claude_spec(layout, handover_project, image, uid, gid) -> CommandSpec`
- Produces: `_prepare_claude_project_state(layout: StateLayout) -> None`
- Produces: `RuntimeSpecBuilder` and agent-keyed builder dispatch.

- [ ] **Step 1: Write failing runtime spec tests**

Assert Claude uses the common hardened flags and exact mounts:

```text
/state/workspaces/agent-container -> /workspace (rw)
/state/projects/agent-container/claude-config -> /home/agent/.claude (rw)
/state/shared-auth/claude/.credentials.json -> /home/agent/.claude/.credentials.json (rw)
/state/projects/agent-container/cache -> /home/agent/.cache (rw)
/state/gh -> /home/agent/.config/gh (ro)
/vault/handovers/agent-container -> /handovers/agent-container (rw)
```

Also assert `CLAUDE_CONFIG_DIR`, project/handover env, final command `claude`, and absence of `dangerously-skip-permissions`.

- [ ] **Step 2: Verify RED and implement `run_claude_spec()`**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_podman -v`

Expected: missing runtime function.

Reuse `_runtime_prefix()` and `_git_environment_args()`. Reject uid/gid mismatch exactly like Codex; add only the six approved mounts and three approved environment variables.

- [ ] **Step 3: Write failing lazy-state and dispatch tests**

Extend the runtime fixture with private Claude auth. For `run p --agent claude`, assert image preflight precedes creation of `claude-config`, config mode is `0700`, the Claude builder runs, and stdout is exactly `Starting Claude for project: p\n`. When image-exists returns `29`, assert no config is created. Add broad/symlinked auth and config rejection cases with secret-marker suppression. In `AgentCtlProjectTest`, also assert a successful `project add` does not create `claude-config`; only first Claude run or migration may create it.

- [ ] **Step 4: Verify RED and implement dispatch**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlRunDoctorTest -v`

Expected: Claude cases FAIL.

Define:

```python
RuntimeSpecBuilder = Callable[[StateLayout, Path, str, int, int], CommandSpec]

def _prepare_claude_project_state(layout: StateLayout) -> None:
    ensure_private_directory(layout.claude_config, create=True)
```

Refactor runtime preflight to validate common state and selected-agent auth. Permit only a missing Claude config before image preflight. Inject `runtime_spec_builders: Mapping[str, RuntimeSpecBuilder] | None`, default to Codex/Claude functions, create Claude config only after successful image preflight, then run the selected builder.

- [ ] **Step 5: Verify GREEN and commit**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Expected: all tests PASS.

```bash
git add src/agent_container/podman.py src/agent_container/agentctl.py \
  tests/container/test_podman.py tests/container/test_agentctl.py
git commit -m "feat: run Claude in project isolation"
```

---

### Task 5: Agent-aware doctor checks

**Files:**
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_agentctl.py`

**Interfaces:**
- Consumes: `cli_version_spec()` and Tasks 1-4 state paths.
- Produces: `_doctor(project_id, agent, image, environment, runner, git_remote_reader) -> list[CheckResult]`

- [ ] **Step 1: Write failing doctor ordering tests**

Define exact expected orders:

```python
CODEX_DOCTOR = [
    "podman-version", "podman-rootless", "image", "codex-version",
    "private-state", "codex-auth", "gh-hosts", "project-metadata",
    "workspace-origin", "handover-project", "network-policy",
]
CLAUDE_DOCTOR = [
    "podman-version", "podman-rootless", "image", "claude-version",
    "private-state", "claude-auth", "claude-config", "gh-hosts",
    "project-metadata", "workspace-origin", "handover-project", "network-policy",
]
ALL_DOCTOR = [
    "podman-version", "podman-rootless", "image", "codex-version",
    "claude-version", "private-state", "codex-auth", "claude-auth",
    "claude-config", "gh-hosts", "project-metadata", "workspace-origin",
    "handover-project", "network-policy",
]
```

Test each mode. For `all`, assert one common Podman probe set, both version probes, fixed order, no secret markers, and nonzero exit if Claude auth/config is absent.

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlRunDoctorTest -v`

Expected: new order and Claude cases FAIL.

- [ ] **Step 3: Implement common-once, agent-specific checks**

Pass `agent` into `_doctor()`. Convert `all` to `("codex", "claude")`; otherwise use one selected agent. Run Podman version/rootless/image once. If image exists, run `cli_version_spec()` per selected agent and report `<agent>-version` without stderr.

Validate only common plus selected-agent directories. Check `codex_auth_file` or `claude_auth_file`; for Claude also call `ensure_private_directory(layout.claude_config)`. Keep GitHub, metadata, origin, handover, and network checks common. Change WARN detail to `outbound network is not domain-restricted in Phase 2`.

- [ ] **Step 4: Verify GREEN and commit**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlRunDoctorTest -v`

Expected: PASS.

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Expected: all tests PASS.

```bash
git add src/agent_container/agentctl.py tests/container/test_agentctl.py
git commit -m "feat: diagnose Codex and Claude state"
```

---

### Task 6: Safe allowlisted migration core

**Files:**
- Create: `src/agent_container/migration.py`
- Create: `tests/container/test_migration.py`

**Interfaces:**
- Produces: frozen `MigrationEntry(relative_path: Path, is_directory: bool, executable: bool)`
- Produces: frozen `GeneratedFile(relative_path: Path, body: bytes, executable: bool = False)`
- Produces: frozen `MigrationPlan(source, destination, entries, generated_files, skipped)`
- Produces: `plan_claude_migration(source: Path, destination: Path) -> MigrationPlan`
- Produces: `apply_claude_migration(plan: MigrationPlan) -> Path`
- Produces: `render_migration_plan(plan: MigrationPlan) -> tuple[str, ...]`

- [ ] **Step 1: Write failing allowlist and credential-setting tests**

Create fixtures with safe `CLAUDE.md`, `settings.json`, `hooks/run.sh`, and `skills/demo/SKILL.md`, plus denied credentials, sessions, handovers, cache, logs, and `.git` containing `DO-NOT-PRINT-CREDENTIAL-BODY`.

Assert selected relative paths are exactly:

```python
{
    "CLAUDE.md", "settings.json", "hooks", "hooks/run.sh",
    "skills", "skills/demo", "skills/demo/SKILL.md",
}
```

Assert rendered output lacks the marker. Add failures for non-object settings, `apiKeyHelper` at any nesting level, and `env` names containing `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`, `API_KEY`, or `AUTH`; the value must never appear in exceptions.

- [ ] **Step 2: Add failing filesystem-boundary tests and verify RED**

Cover relative source, symlinked source, source resolving differently from its written absolute path, symlink/FIFO inside an allowlist root, source escape, and existing destination.

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_migration -v`

Expected: import error because `migration.py` is absent.

- [ ] **Step 3: Implement immutable planning**

Use exact constants:

```python
DEFAULT_FILES = ("CLAUDE.md", "settings.json")
DEFAULT_DIRECTORIES = ("agents", "commands", "rules", "skills", "hooks")
DENIED_TOP_LEVEL = frozenset({
    ".credentials.json", ".claude.json", ".git", "projects", "sessions",
    "transcripts", "handovers", "plans", "state", "cache", "logs",
    "test-results", "scratchpad",
})
SECRET_ENV_KEY = re.compile(
    r"TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY|AUTH", re.IGNORECASE
)
```

Require an absolute, non-symlink, real directory whose strict resolved path equals the provided path. Reject an existing destination. Walk only allowlist roots with `iterdir()`, inspect `lstat()` before reads, accept only regular files/directories, sort POSIX relative paths, and re-check every strict resolution remains under source.

Parse `settings.json` as an object. Recursively reject key `apiKeyHelper`; under every `env` object reject matching names without including values in errors.

- [ ] **Step 4: Write failing atomic-apply tests**

Assert apply creates directories at `0700`, files at `0600`, and source-user-executable files at `0700`; source bytes/modes stay unchanged. Assert destination stays absent when a planned file becomes a symlink, when destination appears after planning, or when patched `shutil.copyfile` raises `OSError`. Assert no `.migrate-*` stage remains.

- [ ] **Step 5: Implement staged atomic apply and safe rendering**

Create the stage under `destination.parent` with:

```python
stage = Path(tempfile.mkdtemp(
    prefix=f".{destination.name}.migrate-",
    dir=destination.parent,
))
```

Revalidate destination and every source entry immediately before copy. Use `shutil.copyfile`, apply exact modes, write generated files exclusively, and finish with `stage.rename(destination)`. On error, remove only the known resolved stage and re-raise.

Render only `COPY file RELATIVE`, `COPY executable RELATIVE`, `SKIP denied RELATIVE`, and `DESTINATION ABSOLUTE`; never render bodies, values, hashes, sizes, uid/inode, or timestamps.

- [ ] **Step 6: Verify GREEN and commit**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_migration -v`

Expected: PASS.

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Expected: all tests PASS.

```bash
git add src/agent_container/migration.py tests/container/test_migration.py
git commit -m "feat: plan and apply safe Claude migrations"
```

---

### Task 7: Explicit plugin migration and migrate command

**Files:**
- Modify: `src/agent_container/migration.py`
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_migration.py`
- Modify: `tests/container/test_agentctl.py`

**Interfaces:**
- Produces: `add_plugin_entries(plan: MigrationPlan, plugin_ids: tuple[str, ...]) -> MigrationPlan`
- Produces working `agentctl migrate claude PROJECT --from PATH [--plugin ID]... [--apply]`.

- [ ] **Step 1: Write failing version-2 plugin manifest tests**

Create `plugins/installed_plugins.json` with:

```python
{
    "version": 2,
    "plugins": {
        "issue-ops@local-marketplace": [{
            "scope": "user",
            "installPath": str(
                source / "plugins/cache/local-marketplace/issue-ops/1.2.3"
            ),
            "version": "1.2.3",
        }],
        "unselected@local-marketplace": [{
            "scope": "user",
            "installPath": str(
                source / "plugins/cache/local-marketplace/unselected/9.9.9"
            ),
            "version": "9.9.9",
        }],
    },
}
```

Create matching cache trees and `known_marketplaces.json`. Assert exact selection copies only `1.2.3` and generates filtered manifests without `unselected`. Add failures for unknown ID, version other than 2, malformed/multiple records, cache path outside source, symlinked cache, missing marketplace metadata, and secret-free errors.

- [ ] **Step 2: Verify RED and implement plugin selection**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_migration -v`

Expected: missing `add_plugin_entries()`.

Implementation rules:

1. Empty IDs return the original plan.
2. Root schema must be `{version: 2, plugins: object}`.
3. Each exact ID has one record with string `installPath` and `version`.
4. Strict install path stays inside `source/plugins/cache` without symlink components.
5. Add only the exact cache tree through Task 6 validators.
6. Retain only selected marketplace keys from `known_marketplaces.json`.
7. Generate filtered manifests via `json.dumps(..., ensure_ascii=False, indent=2, sort_keys=True) + "\n"`.
8. Sort/deduplicate entries and generated files in a new frozen plan.

- [ ] **Step 3: Write failing migrate orchestration tests**

Create a valid private project-state fixture and assert dry-run prints plan plus `MODE dry-run` without creating `claude-config`; `--apply` creates only selected state and prints `MODE apply`. Assert invalid plugin is rejected before filesystem access, missing metadata/source/existing destination fail, secret markers stay hidden, and runner calls remain zero.

- [ ] **Step 4: Verify RED and implement command orchestration**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlMigrationTest -v`

Expected: migration dispatch missing.

In `main()`: validate project/plugin IDs, enforce exact state root, validate root/projects/project directory and `project.json`, plan migration to `layout.claude_config`, add selected plugins, print safe plan plus mode, and call apply only with `--apply`. Never call Podman.

- [ ] **Step 5: Verify GREEN and commit**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_migration tests.container.test_agentctl.AgentCtlMigrationTest -v`

Expected: PASS.

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Expected: all tests PASS.

```bash
git add src/agent_container/migration.py src/agent_container/agentctl.py \
  tests/container/test_migration.py tests/container/test_agentctl.py
git commit -m "feat: migrate selected Claude configuration"
```

---

### Task 8: Operations docs, automated verification, and host smoke test

**Files:**
- Create: `docs/phase2-claude-code.md`
- Create: `docs/phase2-smoke-test.md`
- Modify: `README.md`
- Modify: `tests/container/test_docs.py`
- Modify: `.containerignore` only if actual Containerfile COPY inputs change.

**Interfaces:**
- Consumes all Tasks 1-7 CLI contracts.
- Produces operator flow, migration review flow, smoke checklist, and final evidence.

- [ ] **Step 1: Write failing documentation contract tests**

Add `Phase2DocumentationTest` asserting the operator guide contains:

```python
for command in (
    "agentctl build", "agentctl auth claude", "agentctl migrate claude",
    "--agent claude", "--agent all",
):
    self.assertIn(command, body)
for boundary in (
    "~/.claude", ".credentials.json", "0700", "0600", "dry-run",
    "外向き通信はドメイン制限されていません",
):
    self.assertIn(boundary, body)
self.assertIn("credential本文を表示しません", body)
self.assertIn("旧claude-containerを変更しません", body)
```

Assert the smoke guide contains `利用者承認`, `mainへ直接pushしない`, `credential本文を表示しない`, `claude auth status`, `認証更新`, and `旧claude-container`.

- [ ] **Step 2: Verify RED and write docs**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_docs.Phase2DocumentationTest -v`

Expected: Phase 2 files missing.

Write `docs/phase2-claude-code.md` in this order: prerequisites/network WARN; latest build and rollback overrides; Claude login; migration dry-run then apply before first Claude run; doctor; run/resume; exact mounts; troubleshooting; rollback to Codex or unchanged old container.

Write `docs/phase2-smoke-test.md` with `not run` observations for: rootless, build and both versions, pre-auth doctor, approved login, disposable migration, Claude doctor, mount inspection, Claude edit/test/commit on a test branch, restart/resume, credential refresh metadata only, Codex regression, and proof old container is unchanged. Exclude merge, force-push, release, and deletion.

Link design/guide/smoke docs from README. Do not broaden `.containerignore` unless build COPY inputs require it.

- [ ] **Step 3: Verify docs, all tests, and diff**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_docs -v`

Expected: PASS.

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Expected: all tests PASS.

Run: `git diff --check`

Expected: exit 0 with no output.

- [ ] **Step 4: Commit docs before host mutation**

```bash
git add README.md docs/phase2-claude-code.md docs/phase2-smoke-test.md \
  tests/container/test_docs.py
git commit -m "docs: add Phase 2 Claude operations"
```

- [ ] **Step 5: Obtain approval for networked/interactive checks**

Explain that build downloads current packages, then request approval for:

```bash
bin/agentctl build
bin/agentctl auth claude
bin/agentctl doctor agent-container --agent all
```

Never display or record browser codes, tokens, credential bodies, or environment values.

- [ ] **Step 6: Execute the approved smoke checklist**

Follow `docs/phase2-smoke-test.md` exactly. Obtain separate authorization before any GitHub mutation, use a non-main dedicated test branch, do not merge, and do not delete. Inspect mount sources without reading credentials.

For credential refresh, record only owner/mode, whether mtime/inode changed, exit status, and write/rename error presence. If nested file mount refresh fails, stop and report Phase 2 incomplete; never copy credentials per project.

- [ ] **Step 7: Record observations and rerun verification**

Replace only executed `not run` cells with date, exit code, and secret-free evidence; leave skipped checks as `not run`.

Run:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
git diff --check
git status --short --branch
```

Expected: all tests PASS; only smoke observations modified; diff check exits 0.

- [ ] **Step 8: Commit observations and perform final branch verification**

```bash
git add docs/phase2-smoke-test.md
git commit -m "docs: record Phase 2 Claude smoke results"
PYTHONPATH=src python3 -m unittest discover -s tests -v
git diff --check main...HEAD
git status --short --branch
git log --oneline --decorate main..HEAD
```

Expected: zero failures; clean diff; clean feature worktree; focused commits for state/CLI, build, auth, runtime, doctor, migration core, plugin migration, docs, and smoke observations.

After these checks pass, use `superpowers:requesting-code-review`, resolve feedback with `superpowers:receiving-code-review`, rerun final verification, then use `superpowers:finishing-a-development-branch`. Do not push, open a PR, merge, or remove the worktree without explicit user authorization at that stage.
