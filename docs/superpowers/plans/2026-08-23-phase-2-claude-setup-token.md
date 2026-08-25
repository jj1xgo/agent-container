# Phase 2 Claude Code Setup-Token Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Claude's shared `.credentials.json` bind mount with a verified setup-token secret, keeping authentication shared while project configuration remains isolated and Claude subprocesses cannot inherit the credential.

**Architecture:** Store one validated `oauth-token` under shared private state, mount it read-only at `/run/secrets/claude-oauth-token`, and use an image-local Python launcher to inject `CLAUDE_CODE_OAUTH_TOKEN` plus mandatory subprocess scrubbing immediately before `exec` of Claude. Generate the token in an ephemeral tmpfs config, collect it through a hidden host prompt, verify a private staging file with `claude auth status`, then atomically replace the active token. Reject project `.credentials.json` files and quarantine allowlisted legacy shared files only after the replacement token is verified.

**Tech Stack:** Python 3 standard library (`getpass`, `os`, `pathlib`, `secrets`, `stat`), `unittest`, rootless Podman, Debian/Node container image, Claude Code CLI, Git.

**Spec:** `docs/superpowers/specs/2026-08-23-phase-2-claude-setup-token-design.md`

## Global Constraints

- Preserve rootless Podman, `--read-only`, `--cap-drop=all`, `no-new-privileges`, keep-id, and bounded `/tmp` tmpfs on every Claude container.
- Never put the token value in Podman argv, the host environment, stdout/stderr, exceptions, fixtures shown by assertion failures, handovers, or project configuration.
- The active token is exactly `<state-root>/shared-auth/claude/oauth-token`: parent directories `0700`, regular non-symlink file `0600`, current-user ownership, printable single-line ASCII, 32–4096 bytes, and no whitespace/control characters.
- The setup-token container gets only ephemeral tmpfs Claude config; it does not mount host Claude config, the active token, a workspace, or handovers.
- Status and runtime containers mount the token read-only at `/run/secrets/claude-oauth-token`; they never mount or copy `.credentials.json`.
- The image-local launcher must set `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` itself. Callers cannot disable or override it.
- A project `.credentials.json`, including an empty file or symlink, is a pre-container hard failure. Project `.claude.json`, backups, cache, sessions, plugins, and memory remain project-scoped.
- Failed setup, cancelled input, invalid input, failed status, or staging error must leave an existing active token byte-for-byte unchanged. A malformed but metadata-safe old token may be rotated; auth preflight must not trap recovery by requiring its contents to be valid.
- Legacy data is moved, never read or deleted, and only after replacement-token status succeeds. Quarantine remains under the private state root.
- Keep Codex behavior and all existing command defaults unchanged. Normal rebuilds continue to resolve both CLIs from `latest`; explicit version overrides remain available for rollback.
- Use TDD. Run each named RED command before implementation, then the corresponding GREEN and regression commands. End every task with a focused commit.
- Preserve the existing uncommitted failed-smoke evidence in `docs/phase2-smoke-test.md`; include it only in the documentation task's intended commit.

---

### Task 1: Token state contract and secret-safe filesystem operations

**Files:**
- Create: `src/agent_container/claude_auth.py`
- Modify: `src/agent_container/state.py`
- Create: `tests/container/test_claude_auth.py`
- Modify: `tests/container/test_state.py`

**Interfaces:**
- Produces: `validate_claude_oauth_token(value: str) -> str`
- Produces: `StateLayout.claude_token_file`, `.claude_legacy_credentials_file`, `.claude_legacy_metadata_file`, `.claude_legacy_backups`, and `.claude_quarantine_root`
- Produces: `stage_claude_token(auth_dir: Path, token: str) -> Path`
- Produces: `install_claude_token(staged: Path, destination: Path) -> None`
- Produces: `discard_staged_token(staged: Path) -> None`
- Produces: `validate_legacy_quarantine_sources(layout: StateLayout) -> tuple[Path, ...]`
- Produces: `quarantine_legacy_claude_state(layout: StateLayout, nonce: str | None = None) -> Path | None`

- [ ] **Step 1: Write failing state-path and token-format tests**

Update `tests/container/test_state.py` to replace the Claude credential path expectation and cover boundary values without embedding a production-looking secret:

```python
from agent_container.state import validate_claude_oauth_token

def test_state_layout_has_setup_token_and_legacy_paths(self) -> None:
    layout = StateLayout(Path("/state"), "agent-container")
    self.assertEqual(
        layout.claude_token_file,
        Path("/state/shared-auth/claude/oauth-token"),
    )
    self.assertEqual(
        layout.claude_legacy_credentials_file,
        Path("/state/shared-auth/claude/.credentials.json"),
    )
    self.assertEqual(
        layout.claude_quarantine_root,
        Path("/state/quarantine/claude"),
    )

def test_claude_oauth_token_accepts_only_safe_single_line_ascii(self) -> None:
    self.assertEqual(validate_claude_oauth_token("x" * 32), "x" * 32)
    self.assertEqual(validate_claude_oauth_token("Z" * 4096), "Z" * 4096)
    for value in ("x" * 31, "x" * 4097, "x y" + "x" * 29,
                  "x\n" + "x" * 31, "é" * 32, "\x7f" + "x" * 31):
        with self.subTest(length=len(value)), self.assertRaises(ValueError):
            validate_claude_oauth_token(value)
```

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_state -v`

Expected: FAIL because the token validator and new paths do not exist.

- [ ] **Step 3: Implement the state contract**

In `state.py`, retain `claude_auth_dir` but replace runtime use of `claude_auth_file` with explicit token and legacy properties. Validate character codes directly so locale and Unicode categories cannot broaden the contract:

```python
def validate_claude_oauth_token(value: str) -> str:
    if not 32 <= len(value) <= 4096 or any(
        ord(character) < 33 or ord(character) > 126 for character in value
    ):
        raise ValueError("Claude OAuth token has invalid format")
    return value

@property
def claude_token_file(self) -> Path:
    return self.claude_auth_dir / "oauth-token"
```

Use exact legacy names `.credentials.json`, `.claude.json`, and `backups`; place quarantine at `root / "quarantine/claude"`. Keep the existing `claude_auth_file` property temporarily as a legacy `.credentials.json` alias so intermediate TDD commits remain green; Task 4 removes it after the last runtime/doctor caller is migrated.

- [ ] **Step 4: Write failing staging, install, and quarantine tests**

Create `tests/container/test_claude_auth.py`. The atomic replacement test should use this concrete shape:

```python
with TemporaryDirectory() as temp:
    auth_dir = Path(temp) / "shared-auth/claude"
    auth_dir.mkdir(parents=True, mode=0o700)
    old = auth_dir / "oauth-token"
    old.write_text("o" * 32, encoding="ascii")
    old.chmod(0o600)
    staged = stage_claude_token(auth_dir, "n" * 32)
    self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o600)
    install_claude_token(staged, old)
    self.assertEqual(old.read_text(encoding="ascii"), "n" * 32)
    self.assertFalse(staged.exists())
```

Add separate tests named `test_invalid_token_creates_no_staging_file`, `test_discard_removes_only_the_exact_staging_file`, and `test_install_rejects_symlinked_destination_and_wrong_parent_mode`. Add quarantine tests named `test_moves_only_allowlisted_legacy_entries_without_reading_bodies`, `test_no_legacy_entries_is_a_noop`, `test_rejects_symlinked_source_or_symlink_inside_backups_before_move`, and `test_quarantine_tree_is_private_and_active_token_is_untouched`.

Use a neutral fixture such as `"x" * 32`; never include it in assertion messages. Patch `Path.read_text` in the quarantine test to raise if called, demonstrating that legacy bodies are not inspected.

- [ ] **Step 5: Verify RED**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_claude_auth -v`

Expected: import failure for the new module.

- [ ] **Step 6: Implement secret-safe file operations**

In `claude_auth.py`:

- validate the auth directory with `ensure_private_directory`;
- create staging with `os.open(..., O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)` using a `secrets.token_hex()` basename in the same directory;
- write through `os.fdopen`, flush, and `os.fsync` before closing;
- revalidate staging with `ensure_private_file` and `validate_claude_oauth_token`;
- make install reject a symlinked/non-private existing destination and call `os.replace(staged, destination)` only after every fallible validation step, so a reported install failure never follows a successful replacement;
- let discard unlink only a regular, non-symlink staging name owned by the current user;
- preflight every allowlisted legacy tree with `lstat`, reject all symlinks recursively, then move exact entries with `os.replace` into `root/quarantine/claude/<nonce>`;
- create all quarantine directories as `0700`, normalize moved directories to `0700` and regular files to `0600` without following links, and never auto-delete quarantine.

Catch filesystem failures only at the CLI boundary; these helpers must not include token contents in error messages.

- [ ] **Step 7: Verify GREEN and regression**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_state tests.container.test_claude_auth -v`

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Expected: all tests PASS after updating old path assertions that intentionally moved to later tasks; no unrelated behavior changes.

- [ ] **Step 8: Commit**

```bash
git add src/agent_container/state.py src/agent_container/claude_auth.py \
  tests/container/test_state.py tests/container/test_claude_auth.py
git commit -m "feat: add private Claude token state"
```

---

### Task 2: Image-local Claude launcher and Podman command boundaries

**Files:**
- Create: `src/agent_container/claude_launcher.py`
- Modify: `src/agent_container/podman.py`
- Create: `tests/container/test_claude_launcher.py`
- Modify: `tests/container/test_podman.py`
- Modify: `tests/container/test_image.py`

**Interfaces:**
- Produces: `load_token(path: Path) -> str`
- Produces: `exec_claude(token_path: Path, arguments: Sequence[str], execvpe: Callable = os.execvpe) -> NoReturn`
- Produces: `claude_setup_token_spec(image: str) -> CommandSpec`
- Produces: `claude_token_status_spec(token_file: Path, image: str) -> CommandSpec`
- Changes: `run_claude_spec(...)` mounts `StateLayout.claude_token_file` and invokes the launcher.

- [ ] **Step 1: Write failing launcher tests**

Create `tests/container/test_claude_launcher.py` using a fake `execvpe` that records only booleans and non-secret fields:

```python
def fake_execvpe(program, argv, environment):
    observed["program"] = program
    observed["argv"] = argv
    observed["has_token"] = "CLAUDE_CODE_OAUTH_TOKEN" in environment
    observed["scrub"] = environment["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"]
    raise ExecObserved

self.assertEqual(observed, {
    "program": "claude",
    "argv": ("claude", "auth", "status"),
    "has_token": True,
    "scrub": "1",
})
```

Also test missing files, symlinks, non-regular files, wrong mode/owner (mock `os.getuid` for owner), invalid format, empty command arguments, and that stdout/stderr remain empty. Patch `os.open` or inspect its flags to prove `O_NOFOLLOW` is requested.

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_claude_launcher -v`

Expected: import failure for `claude_launcher`.

- [ ] **Step 3: Implement the launcher**

Use an explicit CLI contract so every caller has the same secret path:

```text
python3 -m agent_container.claude_launcher \
  /run/secrets/claude-oauth-token -- claude [ARG ...]
```

`load_token` must open with `os.open(path, os.O_RDONLY | os.O_NOFOLLOW)`, validate `fstat` regular-file/mode/owner metadata before reading, decode strict ASCII, and call `validate_claude_oauth_token`. `exec_claude` must copy `os.environ`, overwrite both credential-related keys, and call:

```python
environment["CLAUDE_CODE_OAUTH_TOKEN"] = token
environment["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] = "1"
execvpe(arguments[0], tuple(arguments), environment)
```

Do not add debug output, exception details containing read bytes, or a code path that accepts the token from argv/environment.

- [ ] **Step 4: Replace old Podman tests with setup-token/status/runtime tests**

In `tests/container/test_podman.py`, replace imports and assertions for `auth_claude_spec` / `claude_login_status_spec`. Assert:

```python
setup = claude_setup_token_spec(IMAGE)
self.assertEqual(setup.argv[-2:], ("claude", "setup-token"))
self.assertIn("--tmpfs=/home/agent/.claude:rw,nosuid,nodev,noexec,size=16m", setup.argv)
self.assertNotIn("--mount", setup.argv)

status = claude_token_status_spec(Path("/private/staged"), IMAGE)
joined = " ".join(status.argv)
self.assertIn(
    "src=/private/staged,dst=/run/secrets/claude-oauth-token,ro=true", joined
)
self.assertIn("CLAUDE_CONFIG_DIR=/home/agent/.claude", joined)
self.assertEqual(status.argv[-3:], ("claude", "auth", "status"))
```

For runtime, assert the active token is mounted `ro=true`, `.credentials.json` is absent, the final argv passes through `python3 -m agent_container.claude_launcher`, project config remains mounted read-write, and no token value or `CLAUDE_CODE_OAUTH_TOKEN=` appears in argv or `CommandSpec.environment`.

- [ ] **Step 5: Verify RED**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_podman -v`

Expected: FAIL for missing specs and the old credential mount.

- [ ] **Step 6: Implement hardened Podman specs**

Remove `_claude_auth_prefix`, `auth_claude_spec`, and `claude_login_status_spec`. Add constants for the in-container token and launcher prefix to prevent path drift. `claude_setup_token_spec` adds ephemeral config tmpfs and `CLAUDE_CONFIG_DIR` to `_runtime_prefix`; `claude_token_status_spec` adds the same tmpfs plus a read-only token mount. Both retain interactive TTY because setup output must go directly to the user's private terminal.

Change `run_claude_spec` mounts to:

```python
(layout.claude_token_file, "/run/secrets/claude-oauth-token", True)
```

and append image/launcher/Claude argv in this order:

```python
argv += [image, "python3", "-m", "agent_container.claude_launcher",
         "/run/secrets/claude-oauth-token", "--", "claude"]
```

Do not set `CLAUDE_CODE_OAUTH_TOKEN` or the scrub variable in the Podman spec; the launcher owns both.

- [ ] **Step 7: Prove the launcher is in the image context**

Update `tests/container/test_image.py` to assert `Containerfile` still copies `src` to `/opt/agent-container/src`, sets `PYTHONPATH`, and does not `COPY` any credential/token file. No new `Containerfile` instruction is required because the module lives under the existing `COPY src` tree.

- [ ] **Step 8: Verify GREEN and regression**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_claude_launcher tests.container.test_podman tests.container.test_image -v`

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Expected: all tests PASS; Codex Podman specs are byte-for-byte unchanged.

- [ ] **Step 9: Commit**

```bash
git add src/agent_container/claude_launcher.py src/agent_container/podman.py \
  tests/container/test_claude_launcher.py tests/container/test_podman.py \
  tests/container/test_image.py
git commit -m "feat: launch Claude with scrubbed setup token"
```

---

### Task 3: Interactive setup-token orchestration and atomic activation

**Files:**
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_agentctl.py`

**Interfaces:**
- Adds to `main(...)`: `stdin: TextIO = sys.stdin` and `token_reader: Callable[[str], str] | None = None`, with the reader resolved to `getpass.getpass` inside `main`
- Produces: `_require_private_token_terminal(stdin: TextIO, stderr: TextIO) -> None`
- Produces: `_suppressed_run(runner, spec) -> CompletedProcess[str]`
- Changes: `agentctl auth claude` runs setup, hidden input, staging verification, atomic activation, and legacy quarantine.

- [ ] **Step 1: Replace old Claude-auth success tests with the new happy-path test**

Update the Claude auth test fixture to start with an existing valid active token and allowlisted legacy files. Inject a neutral replacement from `token_reader` and have the fake runner return success. Assert:

- calls are preflight version/rootless/image, `claude setup-token`, and launcher-backed `claude auth status`;
- the status mount source is a private staging path, not the active path;
- the active file contains the replacement only after status returns 0;
- legacy paths moved under `quarantine/claude`, their bodies were never printed, and the old token was not placed in quarantine;
- stdout/stderr do not contain the replacement, existing fixture, prompt response, status output, or runner stderr.

- [ ] **Step 2: Write failing preservation and ordering tests**

Add focused tests named `test_claude_auth_setup_failure_does_not_prompt_or_change_existing_token`, `test_claude_auth_cancelled_prompt_preserves_existing_token`, `test_claude_auth_invalid_token_never_runs_status_and_preserves_existing`, `test_claude_auth_status_failure_discards_stage_and_preserves_existing`, `test_claude_auth_missing_image_creates_no_state_and_does_not_prompt`, `test_claude_auth_rejects_unsafe_existing_token_metadata_before_container`, `test_claude_auth_allows_replacing_malformed_existing_token`, `test_claude_auth_requires_private_tty_for_default_reader`, `test_claude_auth_status_output_is_suppressed_with_real_run_command_adapter`, and `test_claude_auth_validates_legacy_sources_before_setup_and_moves_after_status`.

For cancellation, make `token_reader` raise `EOFError` and `KeyboardInterrupt` in subtests; map both to a general error without traceback or secret output. Record a copy of the old token bytes before calling `main` and compare after every failure.

- [ ] **Step 3: Verify RED**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlBuildAuthTest -v`

Expected: FAIL because auth still invokes `claude auth login` and expects `.credentials.json`.

- [ ] **Step 4: Implement hidden prompt and transaction ordering**

Import `getpass` as a module so the default is resolved at call time, or inject a wrapper; do not bind a patch-resistant default unintentionally. The Claude auth branch must have this order:

```python
_validate_existing_claude_auth(layout)
legacy_sources = validate_legacy_quarantine_sources(layout)
_podman_preflight(runner, image_required=arguments.image)
_prepare_claude_auth(layout)
_require_success(runner(claude_setup_token_spec(arguments.image)), setup_spec)
reader = getpass.getpass if token_reader is None else token_reader
token = reader("Paste Claude setup token (input hidden): ")
staged = stage_claude_token(layout.claude_auth_dir, token)
try:
    status_spec = claude_token_status_spec(staged, arguments.image)
    _require_success(_suppressed_run(runner, status_spec), status_spec)
    install_claude_token(staged, layout.claude_token_file)
    quarantine_legacy_claude_state(layout)
finally:
    discard_staged_token(staged)
```

Use the preflight result to ensure the quarantine source set cannot change unnoticed: either pass the validated exact tuple into the move helper or revalidate and compare immediately before moving. Wipe the local token reference in `finally` as a best-effort lifetime reduction, without claiming Python memory erasure.

When `runner is run_command`, `_suppressed_run` must call `run_command(spec, check=False, capture_output=True)` and discard both streams. For an injected runner, consume only its return code and never forward its stream fields.

- [ ] **Step 5: Update validation helpers and error handling**

`_prepare_claude_auth` creates only private directories and validates existing `oauth-token` metadata; it does not create a token or require the old contents to be valid. `_validate_existing_claude_auth` validates exact state-root ancestry and active-token metadata if present. When the default reader is used, require `stdin.isatty()` and `stderr.isatty()` before starting `setup-token`; this prevents `getpass` from falling back to echoed input. An injected reader is the test-only seam and does not perform the terminal check. Map prompt cancellation to exit 1 and a generic `error: Claude token input cancelled`; do not print paths for staging/quarantine filesystem failures.

- [ ] **Step 6: Verify GREEN and regression**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl -v`

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Expected: all tests PASS, no secret fixture is present in captured CLI output, and existing Codex auth tests remain unchanged.

- [ ] **Step 7: Commit**

```bash
git add src/agent_container/agentctl.py tests/container/test_agentctl.py
git commit -m "feat: activate verified Claude setup tokens"
```

---

### Task 4: Runtime refusal rules and authenticated doctor

**Files:**
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_agentctl.py`

**Interfaces:**
- Changes: `_runtime_agent_auth_file(layout, "claude")` returns `layout.claude_token_file`
- Produces: `_validate_claude_project_config(layout: StateLayout) -> None`
- Changes: doctor adds launcher-backed `claude-auth-status` while keeping output secret-safe.

- [ ] **Step 1: Update shared runtime fixtures from legacy credentials to setup token**

In `AgentCtlRunDoctorTest._runtime_state`, create `shared-auth/claude/oauth-token` with mode `0600` and a valid neutral value. Remove `.credentials.json` as the valid-path fixture. Remove the temporary `StateLayout.claude_auth_file` legacy alias now that no production caller remains. Update check-order constants so Claude doctor reports:

```text
claude-version
private-state
claude-auth
claude-auth-status
claude-config
claude-project-credentials
gh-hosts
project-metadata
workspace-origin
handover-project
network-policy
```

- [ ] **Step 2: Write failing runtime refusal tests**

Add tests proving Claude `run` invokes no Podman command when project `claude-config/.credentials.json` is:

- a regular empty `0600` file;
- a non-empty file;
- a symlink to any target.

The error must identify the unsupported legacy project credential generically, without reading/printing its body or resolved target. Also test invalid token format, token mode `0644`, and token symlink refusal before Podman preflight.

- [ ] **Step 3: Write failing doctor status and secrecy tests**

Add tests that distinguish:

- valid token metadata + status exit 0 => `PASS  claude-auth` and `PASS  claude-auth-status`;
- valid metadata + status exit nonzero => only status FAIL and overall exit 1;
- missing/invalid token => auth FAIL, status FAIL with `not run: token invalid`, and no status container call;
- project `.credentials.json` => `FAIL  claude-project-credentials` and no credential body output;
- fake status stdout/stderr containing a marker => marker absent from rendered doctor output.

Assert status uses the active token read-only mount and the image-local launcher.

- [ ] **Step 4: Verify RED**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlRunDoctorTest -v`

Expected: FAIL because runtime and doctor still treat `.credentials.json` as valid shared auth and doctor does not probe status.

- [ ] **Step 5: Implement runtime and doctor validation**

Change Claude auth-file selection to `claude_token_file`, run `ensure_private_file`, then validate its contents through a helper whose exception message never includes the value. `_validate_claude_project_config` must inspect only the exact `.credentials.json` entry with `exists() or is_symlink()` and fail regardless of type/content.

Call the project-config check in `_validate_runtime_agent_state` before Podman preflight. In doctor:

- validate token metadata and format for `claude-auth`;
- run `claude_token_status_spec(layout.claude_token_file, image)` only when image and token checks passed;
- execute through `_doctor_run`, which captures real subprocess output, and render only `authenticated` or `command failed`;
- validate the config directory separately;
- emit `claude-project-credentials` PASS only when the exact legacy file is absent.

Do not create a missing Claude config during doctor. Preserve common-check de-duplication for `--agent all` and retain the network-policy WARN.

- [ ] **Step 6: Verify GREEN and regressions**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl -v`

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Expected: all tests PASS; default `run`/`doctor` remain Codex; Codex specs and outputs are unchanged.

- [ ] **Step 7: Commit**

```bash
git add src/agent_container/agentctl.py tests/container/test_agentctl.py
git commit -m "fix: enforce Claude token runtime isolation"
```

---

### Task 5: Operator documentation, rebuilt-image verification, and recovery smoke

**Files:**
- Modify: `docs/phase2-claude-code.md`
- Modify: `docs/phase2-smoke-test.md`
- Modify: `docs/codex-operations.md` only where the shared rebuild/version policy is described
- Modify: `README.md` only if its current command examples mention Claude credentials or authentication

**Interfaces:**
- Documents: private setup-token ceremony, token rotation/failure semantics, subprocess scrub, doctor meanings, legacy quarantine, rollback, and latest-on-rebuild policy.
- Verifies: real status, minimal inference, subprocess non-exposure, project isolation, restart/resume, Codex regression, and preservation of the old container.

- [ ] **Step 1: Update operator documentation from `.credentials.json` to `oauth-token`**

Document the exact user flow:

```bash
bin/agentctl build
bin/agentctl auth claude
bin/agentctl doctor agent-container --agent claude
bin/agentctl run agent-container --agent claude
```

State clearly that `claude setup-token` displays a one-year inference-only token in the user's private terminal, the subsequent prompt is hidden, Remote Control is unavailable with this credential, a failed replacement leaves the previous token active, and users must not paste the token into chat, handovers, shell history, screenshots, or logs.

Document that every normal rebuild resolves the newest published Codex and Claude versions because both defaults are `latest` and the cachebuster invalidates the CLI-install layer. Preserve the explicit `--codex-version` / `--claude-version` rollback examples.

- [ ] **Step 2: Convert the smoke checklist into a safe recovery procedure**

Keep the already-recorded failed nested-mount evidence as historical diagnosis. Add a new setup-token section with explicit stop conditions and no secret-output commands. Before any state change, record metadata and exact paths only (`lstat`/mode/owner/type; no `cat`, JSON parser, checksum, prefix, or length output).

The procedure must say that Codex may execute public build/tests, but the user personally completes the `setup-token` display and hidden paste in a private terminal. Do not run the ceremony through captured automation or this conversation.

- [ ] **Step 3: Run the complete automated suite before touching live state**

Run:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
git diff --check
```

Expected: all tests PASS and no whitespace errors.

- [ ] **Step 4: Rebuild normally and record resolved CLI versions**

Run: `bin/agentctl build`

Expected: cachebuster forces the CLI install layer; output reports the current resolved Codex and Claude versions. Do not pass fixed versions for this normal rebuild.

Run the documented image invariant checks and verify the pre-existing old Claude container/image identifiers are unchanged; do not stop, rename, remove, or rebuild them.

- [ ] **Step 5: Have the user perform the private authentication ceremony**

The user runs `bin/agentctl auth claude` in a private terminal and pastes the shown setup token only into the hidden prompt. Record only command exit status and sanitized PASS/FAIL results. If setup, prompt, format, or status fails, stop: confirm the previous active token remains and legacy files have not moved.

- [ ] **Step 6: Verify status, minimal inference, and subprocess scrubbing**

Run doctor and a minimal real Claude inference through the launcher. A successful status alone is insufficient: require a successful API response because a stale/revoked token can pass local status yet receive HTTP 401 on inference.

From a Claude Bash subprocess, test only booleans:

```text
CLAUDE_CODE_OAUTH_TOKEN present: false
Anthropic/cloud credential variables present: false
```

Never print environment entries, values, prefixes, lengths, process environments, or `/run/secrets/claude-oauth-token` contents. Confirm `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` behavior and Linux subprocess PID isolation without weakening container flags.

- [ ] **Step 7: Recover the failed-smoke project state only after new auth succeeds**

Confirm the exact project artifact is `<state-root>/projects/agent-container/claude-config/.credentials.json`, reject it if it or any quarantine target is a symlink, and move it—without reading it—into a new `0700` state-root quarantine directory with file mode `0600`. Do not move project `.claude.json`, project backups, project cache, sessions, plugins, or memory. Do not delete any quarantine.

Shared `.credentials.json`, `.claude.json`, and backups should already have been quarantined by successful `auth claude`; verify their absence from shared auth and presence in private quarantine using metadata only.

- [ ] **Step 8: Complete the end-to-end Claude and Codex smoke**

On a non-main branch, use Claude to make the checklist's harmless change, run the focused test, create a local commit, exit, restart, and resume the session. Verify:

- project config contains no `.credentials.json`;
- only the token file is shared across projects;
- another project cannot observe this project's session/config;
- `doctor --agent all` has all required PASS entries and only the expected network WARN;
- Codex auth/run/doctor still succeed;
- the old Claude container remains unchanged.

Do not push, open a PR, merge, force-push, release, delete state, or delete quarantine.

- [ ] **Step 9: Run final verification and inspect the branch**

Run:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
git diff --check
git status --short
git log --oneline --decorate -12
```

Expected: all tests PASS; only intended documentation/smoke evidence is uncommitted before the docs commit; no credential/token file is tracked or shown.

- [ ] **Step 10: Commit documentation and sanitized smoke evidence**

```bash
git add docs/phase2-claude-code.md docs/phase2-smoke-test.md \
  docs/codex-operations.md
git add README.md  # only if Step 1 required a real README change
git diff --cached --check
git commit -m "docs: complete Claude setup-token operations"
```

- [ ] **Step 11: Whole-branch review checkpoint**

Review every commit from the Phase 2 base through HEAD against both the superseding setup-token spec and the retained portions of the original Phase 2 spec. Confirm specifically:

- no token value crosses argv/host-env/log boundaries;
- setup uses ephemeral config and activation is status-gated/atomic;
- launcher enforces scrub and token read safety;
- project credential copies are rejected;
- legacy entries are quarantined only after success and never deleted;
- latest-on-normal-rebuild behavior and Codex regressions are covered;
- host smoke includes real inference, subprocess non-exposure, resume, and old-container preservation.

If review changes code, rerun the full suite and relevant host smoke before claiming completion.
