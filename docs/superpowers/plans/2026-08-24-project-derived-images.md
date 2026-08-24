# Project Derived Images Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely build and reuse project-specific images from declarative `.agent-container.d/packages.txt` and `.agent-container.d/node-version.txt` inputs.

**Architecture:** A focused `project_image.py` module validates configuration, computes a content key, and renders a minimal generated build context. `agentctl run` resolves or builds the derived image; `doctor` performs the same resolution read-only and reports current, stale, missing, or unconfigured state.

**Tech Stack:** Python dataclasses, SHA-256, temporary build contexts, Containerfile, rootless Podman, Python `unittest`

**Spec:** `docs/superpowers/specs/2026-08-24-debian-project-images-claude-sandbox-design.md`

## Global Constraints

- Only `.agent-container.d/packages.txt` and `.agent-container.d/node-version.txt` are accepted.
- Arbitrary Dockerfiles, scripts, unknown entries, symlinks, special files, traversal, shell syntax, and leading options are rejected before Podman runs.
- `.claude-container.d/` is unsupported and causes an actionable validation result; it is never consumed.
- Derived identity includes immutable base image ID, schema version, normalized packages, project Node version, and target architecture.
- Build context contains generated files only; never copy workspace, `.git`, auth, session, cache, or handover content.
- `run` may build a missing derived image; `doctor` is read-only.
- Build failure never falls back to a stale image.

## File Structure

- Create `src/agent_container/project_image.py`: config validation, normalization, hash, image name, and minimal context rendering.
- Create `tests/container/test_project_image.py`: exhaustive pure-unit coverage for the module.
- Modify `src/agent_container/podman.py`: image inspect, architecture, and derived build command specs.
- Modify `tests/container/test_podman.py`: exact Podman argv tests.
- Modify `src/agent_container/agentctl.py`: runtime image resolution and doctor reporting.
- Modify `tests/container/test_agentctl.py`: orchestration, fail-closed, and read-only doctor tests.
- Modify `docs/phase2-claude-code.md`: project configuration operator workflow.
- Modify `tests/container/test_docs.py`: documentation contract.

---

### Task 1: Parse and Validate Project Image Configuration

**Files:**
- Create: `src/agent_container/project_image.py`
- Create: `tests/container/test_project_image.py`

**Interfaces:**
- Produces: `ProjectImageConfig(packages: tuple[str, ...], node_version: str | None)`.
- Produces: `load_project_image_config(workspace: Path) -> ProjectImageConfig`.
- Produces: `ProjectImageConfig.is_empty -> bool`.

- [ ] **Step 1: Write failing happy-path tests**

```python
def test_loads_normalized_packages_and_node_version(self):
    config_dir = self.workspace / ".agent-container.d"
    config_dir.mkdir()
    (config_dir / "packages.txt").write_text(
        "# build deps\nmake\n gcc \nmake\nlibpng-dev=1.6.48-1\n",
        encoding="utf-8",
    )
    (config_dir / "node-version.txt").write_text("22.23.1\n", encoding="utf-8")

    config = load_project_image_config(self.workspace)

    self.assertEqual(config.packages, ("gcc", "libpng-dev=1.6.48-1", "make"))
    self.assertEqual(config.node_version, "22.23.1")
    self.assertFalse(config.is_empty)
```

Also test an absent directory returns `ProjectImageConfig((), None)`.

- [ ] **Step 2: Run tests and confirm import failure**

Run: `python3 -m unittest tests.container.test_project_image -v`

Expected: ERROR because `agent_container.project_image` does not exist.

- [ ] **Step 3: Implement the dataclass and happy path**

```python
@dataclass(frozen=True)
class ProjectImageConfig:
    packages: tuple[str, ...]
    node_version: str | None

    @property
    def is_empty(self) -> bool:
        return not self.packages and self.node_version is None


def load_project_image_config(workspace: Path) -> ProjectImageConfig:
    config_root = workspace / ".agent-container.d"
    # Validate root, enumerate exact allowed entries, parse UTF-8 text,
    # normalize package lines, validate the optional Node version.
```

Use a full-match package regex equivalent to `[a-z0-9][a-z0-9+.-]*(?::[a-z0-9]+)?(?:=[A-Za-z0-9.+:~_-]+)?`. Sort and deduplicate packages so equivalent files hash identically. Reuse `validate_version` only if moving it to a dependency-neutral module; otherwise define a focused strict Node release validator here and cover it directly.

- [ ] **Step 4: Write failing rejection tests**

Create subtests for:

```python
cases = {
    "unknown file": ("extra.txt", "x"),
    "leading option": ("packages.txt", "--allow-unauthenticated\n"),
    "shell separator": ("packages.txt", "make;id\n"),
    "command substitution": ("packages.txt", "$(id)\n"),
    "whitespace arguments": ("packages.txt", "make gcc\n"),
    "bad node": ("node-version.txt", "latest\n"),
    "multiple node lines": ("node-version.txt", "22.1.0\n22.2.0\n"),
}
```

Add explicit symlink tests for the workspace, `.agent-container.d`, each allowed file, and an unknown symlink. Add FIFO coverage on Linux with `os.mkfifo`. Add a `.claude-container.d` test that raises `ValueError` without reading its contents.

- [ ] **Step 5: Implement fail-closed filesystem validation**

Use `lstat()`/`stat.S_ISREG` and resolved-parent equality checks. Read at most 64 KiB per configuration file and reject larger files. Decode strict UTF-8. Error messages name the configuration field but never include file contents.

- [ ] **Step 6: Run project-image tests**

Run: `python3 -m unittest tests.container.test_project_image -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/agent_container/project_image.py tests/container/test_project_image.py
git commit -m "feat: validate project image config"
```

### Task 2: Compute Identity and Render a Minimal Build Context

**Files:**
- Modify: `src/agent_container/project_image.py`
- Modify: `tests/container/test_project_image.py`

**Interfaces:**
- Consumes: `ProjectImageConfig`.
- Produces: `project_image_key(base_image_id: str, config: ProjectImageConfig, architecture: str) -> str`.
- Produces: `project_image_name(project_id: str, key: str) -> str`.
- Produces: `write_project_build_context(root: Path, base_image: str, config: ProjectImageConfig) -> Path` returning the generated Containerfile path.

- [ ] **Step 1: Write failing deterministic identity tests**

```python
first = project_image_key("sha256:base", ProjectImageConfig(("gcc", "make"), "22.23.1"), "amd64")
same = project_image_key("sha256:base", ProjectImageConfig(("gcc", "make"), "22.23.1"), "amd64")
self.assertEqual(first, same)
self.assertRegex(first, r"^[0-9a-f]{64}$")
for changed in (
    project_image_key("sha256:other", config, "amd64"),
    project_image_key("sha256:base", ProjectImageConfig(("make",), "22.23.1"), "amd64"),
    project_image_key("sha256:base", config, "arm64"),
):
    self.assertNotEqual(first, changed)
self.assertEqual(project_image_name("sotlas-frontend", first), f"localhost/agent-container-project:sotlas-frontend-{first[:16]}")
```

- [ ] **Step 2: Run the identity test and confirm failure**

Run: `python3 -m unittest tests.container.test_project_image.ProjectImageIdentityTest -v`

Expected: ERROR because the functions are undefined.

- [ ] **Step 3: Implement canonical hashing**

```python
DERIVED_IMAGE_SCHEMA_VERSION = 1

payload = {
    "architecture": architecture,
    "base_image_id": base_image_id,
    "node_version": config.node_version,
    "packages": list(config.packages),
    "schema": DERIVED_IMAGE_SCHEMA_VERSION,
}
encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
return hashlib.sha256(encoded).hexdigest()
```

Validate `base_image_id`, architecture, project ID, and key before using them in names.

- [ ] **Step 4: Write failing build-context tests**

Assert the generated directory contains exactly `Containerfile` and, only when needed, `packages.txt`. Assert it contains no value from sentinel workspace files such as `DO-NOT-COPY-SECRET`. Assert the Containerfile:

- starts with `ARG BASE_IMAGE` and `FROM ${BASE_IMAGE}`;
- switches to `USER root` for installation and back to `USER agent`;
- uses `xargs --no-run-if-empty apt-get install -y --no-install-recommends` for validated packages;
- downloads project Node from nodejs.org and checks `SHASUMS256.txt`;
- installs under `/opt/project-node`;
- sets `PATH=/opt/project-node/bin:/usr/local/bin:/opt/agent-node/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin` only when project Node is configured.

- [ ] **Step 5: Implement generated context rendering**

Render fixed template fragments only; never interpolate raw shell fragments. Write normalized packages one per line. Use the same architecture mapping and checksum workflow as the common image, with the already validated exact Node version. Create files with modes 0600 and the context directory with 0700.

- [ ] **Step 6: Run project-image tests**

Run: `python3 -m unittest tests.container.test_project_image -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/agent_container/project_image.py tests/container/test_project_image.py
git commit -m "feat: render project image contexts"
```

### Task 3: Add Podman Image Resolution Commands

**Files:**
- Modify: `src/agent_container/podman.py`
- Modify: `tests/container/test_podman.py`

**Interfaces:**
- Produces: `podman_image_id_spec(image: str) -> CommandSpec`.
- Produces: `podman_architecture_spec() -> CommandSpec`.
- Produces: `podman_project_images_spec(project_id: str) -> CommandSpec`.
- Produces: `build_project_image_spec(context: Path, containerfile: Path, base_image: str, image: str) -> CommandSpec`.

- [ ] **Step 1: Write failing exact-argv tests**

```python
self.assertEqual(
    podman_image_id_spec(IMAGE).argv,
    ("podman", "image", "inspect", "--format", "{{.Id}}", IMAGE),
)
self.assertEqual(
    podman_architecture_spec().argv,
    ("podman", "info", "--format", "{{.Host.Arch}}"),
)
self.assertEqual(
    podman_project_images_spec("sotlas-frontend").argv,
    (
        "podman", "images", "--filter",
        "reference=localhost/agent-container-project:sotlas-frontend-*",
        "--format", "{{.Repository}}:{{.Tag}}",
    ),
)
spec = build_project_image_spec(Path("/ctx"), Path("/ctx/Containerfile"), IMAGE, DERIVED)
self.assertIn(f"BASE_IMAGE={IMAGE}", spec.argv)
self.assertEqual(spec.argv[-1], "/ctx")
```

- [ ] **Step 2: Run focused Podman tests and confirm failure**

Run: `python3 -m unittest tests.container.test_podman -v`

Expected: ERROR because the new spec builders are undefined.

- [ ] **Step 3: Implement the four command builders**

Use tuple argv only, resolve context and Containerfile paths, pass `--pull=never` for the derived build so the inspected local base ID and the build input cannot diverge, and never attach mounts or environment secrets.

- [ ] **Step 4: Run Podman tests**

Run: `python3 -m unittest tests.container.test_podman -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/podman.py tests/container/test_podman.py
git commit -m "feat: add project image Podman specs"
```

### Task 4: Resolve Derived Images in Run and Doctor

**Files:**
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_agentctl.py`

**Interfaces:**
- Consumes: project-image module and Podman specs from Tasks 1-3.
- Produces: `_resolve_project_image(layout, base_image, runner, build_missing, stdout) -> ProjectImageResolution`.
- Produces: `ProjectImageResolution(image: str, state: Literal["unconfigured", "current", "stale", "missing"], key: str | None)` in `project_image.py`.

- [ ] **Step 1: Write failing run orchestration tests**

Use a runtime fixture with `.agent-container.d/packages.txt`. Configure the fake runner to return base ID, architecture, missing derived image, successful build, then runtime success. Assert:

```python
self.assertEqual(runtime_spec.image, expected_derived_image)
self.assertEqual(sum(call.argv[:2] == ("podman", "build") for call in calls), 1)
self.assertIn("project image missing; building", stdout.getvalue())
```

Add cache-hit coverage where `podman image exists` returns 0 and no build occurs. Add build-failure coverage proving the runtime spec is never constructed and no base-image fallback occurs.

- [ ] **Step 2: Run focused run tests and confirm failure**

Run: `python3 -m unittest tests.container.test_agentctl.AgentCtlRunDoctorTest -v`

Expected: FAIL because runtime always uses `arguments.image`.

- [ ] **Step 3: Implement resolution with temporary context cleanup**

Flow:

```python
config = load_project_image_config(layout.workspace)
if config.is_empty:
    return ProjectImageResolution(base_image, "unconfigured", None)
base_id = (_required_probe_run(runner, podman_image_id_spec(base_image)).stdout or "").strip()
architecture = (_required_probe_run(runner, podman_architecture_spec()).stdout or "").strip()
key = project_image_key(base_id, config, architecture)
image = project_image_name(layout.project_id, key)
if image_exists(image):
    return ProjectImageResolution(image, "current", key)
if not build_missing:
    prior = _doctor_run(runner, podman_project_images_spec(layout.project_id))
    state = "stale" if prior.returncode == 0 and (prior.stdout or "").strip() else "missing"
    return ProjectImageResolution(image, state, key)
with tempfile.TemporaryDirectory(prefix="agent-container-project-") as temporary:
    containerfile = write_project_build_context(Path(temporary), base_image, config)
    build_spec = build_project_image_spec(
        Path(temporary), containerfile, base_image, image
    )
    _require_success(runner(build_spec), build_spec)
return ProjectImageResolution(image, "current", key)
```

Call this after base-image preflight and before agent-specific state mutation. Pass `resolution.image` to the existing runtime builder.

- [ ] **Step 4: Write failing doctor read-only tests**

Assert doctor reports `project-image current`, `project-image stale`, `project-image missing`, or `project-image unconfigured`; it never calls `podman build`. Add invalid-config coverage that reports FAIL without exposing file contents.

- [ ] **Step 5: Implement doctor resolution**

Call the same resolver with `build_missing=False`. Convert validation and inspection failures to existing `CheckResult` values. Do not create a temporary directory in doctor mode.

- [ ] **Step 6: Run controller tests**

Run: `python3 -m unittest tests.container.test_agentctl -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/agent_container/agentctl.py src/agent_container/project_image.py tests/container/test_agentctl.py
git commit -m "feat: resolve project runtime images"
```

### Task 5: Document and Integrate Project Images

**Files:**
- Modify: `docs/phase2-claude-code.md`
- Modify: `tests/container/test_docs.py`

**Interfaces:**
- Consumes: completed project-image workflow.
- Produces: operator instructions and a real derived-image smoke result.

- [ ] **Step 1: Write a failing documentation contract**

Assert the operator guide contains `.agent-container.d/packages.txt`, `.agent-container.d/node-version.txt`, the unsupported `.claude-container.d` warning, automatic build on `run`, read-only `doctor`, and no runtime package installation.

- [ ] **Step 2: Run documentation tests and confirm failure**

Run: `python3 -m unittest tests.container.test_docs -v`

Expected: FAIL on the missing workflow text.

- [ ] **Step 3: Update documentation without overwriting dirty work**

Inspect `git diff -- docs/phase2-claude-code.md` first. Add exact examples:

```text
.agent-container.d/packages.txt
gcc
libc6-dev
make

.agent-container.d/node-version.txt
22.23.1
```

State that `findsummits` currently needs no project Node pin and `sotlas-frontend` should carry its upstream-required pin in its own repository.

- [ ] **Step 4: Run full unit tests**

Run: `python3 -m unittest discover -s tests -v`

Expected: PASS.

- [ ] **Step 5: Run a disposable derived-image integration smoke**

Create a temporary registered fixture workspace through the existing test helpers or a dedicated temporary state root. Configure `packages.txt` with `make` and `node-version.txt` with a known official release. Run once to build and a second time to confirm cache reuse. Do not modify `findsummits` or `sotlas-frontend` from this repository task.

Expected: first resolution builds one derived image; second resolution reports current and does not build; inside the image `make --version` and project `node --version` succeed while `codex --version` and `claude --version` still use agent Node.

- [ ] **Step 6: Commit**

```bash
git add docs/phase2-claude-code.md tests/container/test_docs.py
git commit -m "docs: explain project image configuration"
```
