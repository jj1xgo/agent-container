# Claude Weaker Nested Sandbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Claude Bash usable inside rootless Podman while enforcing managed credential, hook, MCP, and no-fallback policies and rejecting the design if the parent token is visible through `/proc`.

**Architecture:** Remove the global subprocess scrub that forces the unsupported strong proc sandbox, deploy immutable Claude managed settings plus an empty managed MCP set, and use weaker nested sandbox mode. A dedicated probe emits booleans only and gates live acceptance without printing token material.

**Tech Stack:** Claude Code managed settings, bubblewrap, rootless Podman, Python launcher/probe, JSON, Python `unittest`

**Spec:** `docs/superpowers/specs/2026-08-24-debian-project-images-claude-sandbox-design.md`

## Global Constraints

- Do not set `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` in the Claude launcher.
- Sandbox remains enabled with `enableWeakerNestedSandbox=true`, `allowUnsandboxedCommands=false`, and fail-if-unavailable behavior.
- Bash must not receive `CLAUDE_CODE_OAUTH_TOKEN` and must not read `/run/secrets/claude-oauth-token`.
- Built-in Read must deny the absolute token path using Claude permission-rule syntax.
- All hooks and all MCP servers are disabled initially through immutable managed policy.
- Outer Podman flags and narrow mounts remain unchanged; no capabilities, privileged mode, host PID sharing, or Podman socket are added.
- Acceptance output contains booleans only—never token values, lengths, prefixes, hashes, environment listings, or `/proc/*/environ` contents.
- If parent token visibility through `/proc` is true, stop; do not declare Phase 2 complete.

## File Structure

- Create `profiles/claude/managed-settings.json`: immutable sandbox, credential, permission, hook, and MCP allowlist policy.
- Create `profiles/claude/managed-mcp.json`: empty exclusive MCP configuration.
- Modify `Containerfile`: copy managed policy into `/etc/claude-code`.
- Modify `.containerignore`: allow only the two Claude policy files.
- Modify `tests/container/test_image.py`: validate exact policy and copied paths.
- Modify `src/agent_container/claude_launcher.py`: stop forcing global scrub.
- Modify `tests/container/test_claude_launcher.py`: assert token remains parent-only and scrub is absent.
- Create `src/agent_container/claude_security_probe.py`: boolean-only child environment, token file, and parent `/proc` checks.
- Create `tests/container/test_claude_security_probe.py`: fixture-based no-secret-output tests.
- Create `src/agent_container/claude_policy.py`: validate immutable policy without printing its contents.
- Modify `src/agent_container/agentctl.py`: doctor check for immutable policy availability.
- Modify `src/agent_container/podman.py`: policy probe command spec.
- Modify `tests/container/test_agentctl.py` and `tests/container/test_podman.py`: doctor policy tests.
- Modify `docs/phase2-claude-code.md`, `docs/phase2-smoke-test.md`, and `docs/codex-operations.md`: reconcile existing dirty docs with final constraints and smoke evidence.
- Modify `tests/container/test_docs.py`: documentation contract.

---

### Task 1: Deploy Immutable Claude Managed Policy

**Files:**
- Create: `profiles/claude/managed-settings.json`
- Create: `profiles/claude/managed-mcp.json`
- Modify: `Containerfile`
- Modify: `.containerignore`
- Modify: `tests/container/test_image.py`

**Interfaces:**
- Produces: `/etc/claude-code/managed-settings.json` and `/etc/claude-code/managed-mcp.json` in the image.

- [ ] **Step 1: Write failing exact-policy tests**

```python
settings = json.loads((ROOT / "profiles/claude/managed-settings.json").read_text())
self.assertTrue(settings["sandbox"]["enabled"])
self.assertTrue(settings["sandbox"]["enableWeakerNestedSandbox"])
self.assertFalse(settings["sandbox"]["allowUnsandboxedCommands"])
self.assertTrue(settings["sandbox"]["failIfUnavailable"])
self.assertIn(
    {"name": "CLAUDE_CODE_OAUTH_TOKEN", "mode": "deny"},
    settings["sandbox"]["credentials"]["envVars"],
)
self.assertIn(
    {"path": "/run/secrets/claude-oauth-token", "mode": "deny"},
    settings["sandbox"]["credentials"]["files"],
)
self.assertIn("Read(//run/secrets/claude-oauth-token)", settings["permissions"]["deny"])
self.assertEqual(settings["permissions"]["disableBypassPermissionsMode"], "disable")
self.assertTrue(settings["disableAllHooks"])
self.assertTrue(settings["allowManagedHooksOnly"])
self.assertEqual(settings["allowedMcpServers"], [])
self.assertTrue(settings["allowManagedMcpServersOnly"])
self.assertEqual(json.loads(managed_mcp.read_text()), {"mcpServers": {}})
```

Also assert Containerfile copies both files to `/etc/claude-code/` and `.containerignore` includes only `profiles/claude/**`, not arbitrary profiles.

- [ ] **Step 2: Run image-contract tests and confirm failure**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_image -v`

Expected: FAIL because the managed files do not exist.

- [ ] **Step 3: Create managed settings**

Use exact credential entries for:

```json
[
  {"name": "CLAUDE_CODE_OAUTH_TOKEN", "mode": "deny"},
  {"name": "ANTHROPIC_API_KEY", "mode": "deny"},
  {"name": "ANTHROPIC_AUTH_TOKEN", "mode": "deny"},
  {"name": "AWS_ACCESS_KEY_ID", "mode": "deny"},
  {"name": "AWS_SECRET_ACCESS_KEY", "mode": "deny"},
  {"name": "AWS_SESSION_TOKEN", "mode": "deny"},
  {"name": "GOOGLE_APPLICATION_CREDENTIALS", "mode": "deny"},
  {"name": "GOOGLE_API_KEY", "mode": "deny"},
  {"name": "AZURE_CLIENT_SECRET", "mode": "deny"}
]
```

The complete JSON must include the booleans and permission rules asserted in Step 1. Do not define hooks. Set `managed-mcp.json` exactly to `{"mcpServers": {}}`.

- [ ] **Step 4: Copy the policy into the image**

```dockerfile
COPY profiles/claude/managed-settings.json /etc/claude-code/managed-settings.json
COPY profiles/claude/managed-mcp.json /etc/claude-code/managed-mcp.json
```

Extend `.containerignore` with only `!profiles/claude/`, `!profiles/claude/managed-settings.json`, and `!profiles/claude/managed-mcp.json`.

- [ ] **Step 5: Run image-contract tests**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_image -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add profiles/claude/managed-settings.json profiles/claude/managed-mcp.json Containerfile .containerignore tests/container/test_image.py
git commit -m "feat: enforce Claude managed sandbox policy"
```

### Task 2: Remove the Incompatible Global Scrub

**Files:**
- Modify: `src/agent_container/claude_launcher.py:37-50`
- Modify: `tests/container/test_claude_launcher.py:123-146`

**Interfaces:**
- Consumes: private OAuth token file.
- Produces: Claude parent environment with `CLAUDE_CODE_OAUTH_TOKEN`, without `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`.

- [ ] **Step 1: Replace the old scrub test with a failing absence test**

```python
def fake_execvpe(program, argv, environment):
    observed["has_token"] = "CLAUDE_CODE_OAUTH_TOKEN" in environment
    observed["has_scrub"] = "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB" in environment
    raise ExecObserved

with patch.dict(os.environ, {"CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1"}):
    with self.assertRaises(ExecObserved):
        exec_claude(token_file, ("claude",), fake_execvpe)

self.assertEqual(observed, {"has_token": True, "has_scrub": False})
```

This tests that the launcher removes a caller-provided scrub variable instead of merely declining to add it.

- [ ] **Step 2: Run the focused launcher test and confirm failure**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_claude_launcher.ClaudeLauncherTest.test_exec_claude_sets_parent_token_without_global_scrub -v`

Expected: FAIL because the launcher forces scrub to `1`.

- [ ] **Step 3: Implement scrub removal**

```python
environment = os.environ.copy()
environment.pop("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", None)
environment["IS_DEMO"] = "1"
environment["CLAUDE_CODE_OAUTH_TOKEN"] = token
execvpe(argv[0], argv, environment)
```

- [ ] **Step 4: Run all launcher tests**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_claude_launcher -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/claude_launcher.py tests/container/test_claude_launcher.py
git commit -m "fix: use scoped Claude credential policy"
```

### Task 3: Add a Boolean-Only Security Probe

**Files:**
- Create: `src/agent_container/claude_security_probe.py`
- Create: `tests/container/test_claude_security_probe.py`

**Interfaces:**
- Produces: `ProbeResult(oauth_token_visible: bool, token_file_readable: bool, parent_token_via_proc_readable: bool)`.
- Produces: `run_probe(token_path: Path, proc_root: Path, environment: Mapping[str, str]) -> ProbeResult`.
- Produces: CLI output of exactly three lowercase boolean lines.

- [ ] **Step 1: Write failing fixture-based probe tests**

```python
result = run_probe(
    token_path=fixture / "denied-token",
    proc_root=fixture / "proc",
    environment={},
)
self.assertEqual(result, ProbeResult(False, False, False))
self.assertEqual(
    render(result),
    "oauth_token_visible=false\n"
    "token_file_readable=false\n"
    "parent_token_via_proc_readable=false\n",
)
```

Add fixtures where each condition is individually true. For `/proc`, write `environ` with `CLAUDE_CODE_OAUTH_TOKEN=DO-NOT-PRINT-CREDENTIAL-BODY\0`; assert captured stdout and exception strings never contain the sentinel. Add unreadable/missing/racing proc entries and ensure they are skipped without output.

- [ ] **Step 2: Run probe tests and confirm import failure**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_claude_security_probe -v`

Expected: ERROR because the module does not exist.

- [ ] **Step 3: Implement the probe without retaining secret values**

```python
TOKEN_NAME = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_MARKER = b"CLAUDE_CODE_OAUTH_TOKEN="

@dataclass(frozen=True)
class ProbeResult:
    oauth_token_visible: bool
    token_file_readable: bool
    parent_token_via_proc_readable: bool
```

For the token file, attempt `os.open(path, os.O_RDONLY | os.O_NOFOLLOW)` and close immediately without reading. For proc, iterate numeric directories, open `environ` with `O_NOFOLLOW`, read bounded chunks, and search only for `TOKEN_MARKER`; never return, format, log, or raise read bytes. Treat permission denied, disappeared processes, directories, and malformed entries as unreadable. Exclude the probe's own PID.

- [ ] **Step 4: Implement exact rendering and CLI exit status**

Render only the three lines in Step 1. Exit 0 only when all values are false; exit 1 otherwise. Catch operational errors without including path contents or exception representations in output.

- [ ] **Step 5: Run probe tests**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_claude_security_probe -v`

Expected: PASS and no sentinel in output.

- [ ] **Step 6: Commit**

```bash
git add src/agent_container/claude_security_probe.py tests/container/test_claude_security_probe.py
git commit -m "test: add Claude credential isolation probe"
```

### Task 4: Add Managed-Policy Doctor Checks

**Files:**
- Create: `src/agent_container/claude_policy.py`
- Create: `tests/container/test_claude_policy.py`
- Modify: `src/agent_container/podman.py`
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_podman.py`
- Modify: `tests/container/test_agentctl.py`

**Interfaces:**
- Produces: `claude_policy_status_spec(image: str) -> CommandSpec`.
- Produces: doctor check `claude-managed-policy` with PASS/FAIL only.

- [ ] **Step 1: Write failing hardened command-spec tests**

The policy status command runs `python3 -m agent_container.claude_policy`, is mount-free, read-only, cap-dropped, no-new-privileges, and returns no settings contents. Assert argv contains no credential value and no workspace mount.

- [ ] **Step 2: Run Podman tests and confirm failure**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_podman -v`

Expected: ERROR because `claude_policy_status_spec` is undefined.

- [ ] **Step 3: Write failing policy-content validator tests**

Use temporary JSON files to assert `validate_managed_policy()` returns true only for the exact required sandbox, credential, permission, hook, and MCP restrictions. For every security-critical field, mutate or remove that field in a subtest and assert false. Capture stdout and assert neither fixture sentinel `DO-NOT-PRINT-CREDENTIAL-BODY` nor JSON contents are emitted.

- [ ] **Step 4: Implement the policy validator and command spec**

Create `src/agent_container/claude_policy.py` with `validate_managed_policy(settings_path: Path, mcp_path: Path) -> bool` and a `main()` that checks `/etc/claude-code/managed-settings.json` and `/etc/claude-code/managed-mcp.json`. It compares security-critical fields to constants, prints only `managed_policy_valid=true` or `managed_policy_valid=false`, and exits accordingly. `claude_policy_status_spec` uses the same hardened, mount-free prefix as CLI version probes.

- [ ] **Step 5: Write failing doctor tests**

Assert Claude/all doctor invokes the policy spec, reports PASS on exit 0 and FAIL on nonzero, does not print subprocess stdout/stderr, and Codex-only doctor does not invoke it.

- [ ] **Step 6: Implement doctor integration**

Run the policy check only after base/project image resolution succeeds. Use `_doctor_run`; map result to `CheckResult` without embedding command output.

- [ ] **Step 7: Run policy, controller, and Podman tests**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_claude_policy tests.container.test_podman tests.container.test_agentctl -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/agent_container/podman.py src/agent_container/agentctl.py src/agent_container/claude_policy.py tests/container/test_claude_policy.py tests/container/test_podman.py tests/container/test_agentctl.py
git commit -m "feat: diagnose Claude managed policy"
```

### Task 5: Rebuild and Run the Live Security Gate

**Files:**
- Modify: `docs/phase2-smoke-test.md`
- Modify: `tests/container/test_docs.py`

**Interfaces:**
- Consumes: authenticated Claude runtime and `python3 -m agent_container.claude_security_probe` inside sandboxed Bash.
- Produces: recorded pass/fail evidence with no secret material.

- [ ] **Step 1: Add a failing smoke-document contract**

Assert the smoke guide includes all three exact boolean names, `/sandbox` Config verification, `/hooks`, `/mcp`, the fail-stop rule for parent proc visibility, and the prohibition on secret values, lengths, prefixes, hashes, environments, and proc-environ contents.

- [ ] **Step 2: Run documentation tests and confirm failure**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_docs -v`

Expected: FAIL until the checklist is updated.

- [ ] **Step 3: Reconcile the existing smoke guide**

Inspect the user's current diff first. Replace the obsolete global-scrub/fresh-PID expectation with:

```text
oauth_token_visible=false
token_file_readable=false
parent_token_via_proc_readable=false
```

Document that any `true`, inability to confirm active strict sandbox, loaded hook, or loaded MCP stops the smoke and prevents completion claims.

- [ ] **Step 4: Run the full unit suite before rebuilding**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Expected: PASS.

- [ ] **Step 5: Rebuild with current latest agent versions**

Run: `bin/agentctl build`

Expected: exit 0 and current Node/Codex/Claude versions reported. Do not pin versions unless reproducing a failure.

- [ ] **Step 6: Run doctor before TUI**

Run: `bin/agentctl doctor agent-container-claude-smoke --agent claude`

Expected: rootless Podman, image, Claude version/auth, project state, and `claude-managed-policy` PASS. `project-image` is either unconfigured or current, never silently stale.

- [ ] **Step 7: Run Claude and verify managed UI state**

Run: `bin/agentctl run agent-container-claude-smoke --agent claude`

Inside Claude, inspect `/status`, `/sandbox`, `/hooks`, and `/mcp`. Expected: managed settings source present; sandbox enabled in strict no-fallback mode; weaker nested mode active; no hooks; no MCP servers.

- [ ] **Step 8: Run the dedicated probe through Claude Bash**

Ask Claude to use Bash to run exactly:

```sh
python3 -m agent_container.claude_security_probe
```

Expected exact output:

```text
oauth_token_visible=false
token_file_readable=false
parent_token_via_proc_readable=false
```

If the command cannot run, any value is true, or output includes additional credential-derived data, stop and record FAIL without retrying unsandboxed.

- [ ] **Step 9: Verify normal work and disabled side entrances**

In a disposable workspace fixture, perform a harmless Read/Edit/Write, `git status`, and project build/test. Confirm `/hooks` remains empty and `claude mcp add --transport stdio test -- /bin/true` is rejected by enterprise policy before the command runs.

- [ ] **Step 10: Commit smoke documentation**

```bash
git add docs/phase2-smoke-test.md tests/container/test_docs.py
git commit -m "docs: record Claude sandbox security gate"
```

### Task 6: Reconcile Operations Documentation and Run Codex Regression

**Files:**
- Modify: `docs/phase2-claude-code.md`
- Modify: `docs/codex-operations.md`
- Modify: `tests/container/test_docs.py`

**Interfaces:**
- Consumes: passed Claude live gate and completed common/project image plans.
- Produces: final operator constraints and Codex regression evidence.

- [ ] **Step 1: Write failing final documentation assertions**

Assert operator docs state:

- global scrub is intentionally absent because current Claude forces incompatible strong sandbox behavior;
- hooks and MCP are disabled initially;
- reviewed HTTP MCP may be added only through managed policy;
- stdio MCP remains disabled until global scrub can coexist with nested mode;
- parent `/proc` visibility failure stops operation;
- outer Podman restrictions remain unchanged.

- [ ] **Step 2: Run documentation tests and confirm failure**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_docs -v`

Expected: FAIL on any missing constraint.

- [ ] **Step 3: Reconcile the three dirty documents**

Use `git diff` before editing. Preserve already recorded smoke observations unless the new run supersedes the exact row. Remove claims that `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` or fresh PID isolation currently passes. Do not alter unrelated Codex operations content.

- [ ] **Step 4: Run full tests**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Expected: PASS.

- [ ] **Step 5: Run Codex regression checks on the rebuilt image**

Run: `bin/agentctl doctor agent-container --agent codex`

Run: `bin/agentctl run agent-container --agent codex`

Expected: doctor PASS for required checks; Codex can run sandboxed Bash, edit the workspace, see the handover path notification, and resume its project session. Do not push, open a PR, merge, or expose credentials as part of this regression.

- [ ] **Step 6: Inspect the final diff and secret hygiene**

Run: `git diff --check`

Run: `git grep -nE 'sk-ant-|DO-NOT-PRINT-CREDENTIAL-BODY' -- ':!tests/**' ':!docs/superpowers/**'`

Expected: no whitespace errors; no real token or test sentinel outside intentionally named test/plan fixtures.

- [ ] **Step 7: Commit**

```bash
git add docs/phase2-claude-code.md docs/codex-operations.md tests/container/test_docs.py
git commit -m "docs: finalize nested Claude operations"
```

- [ ] **Step 8: Record a handover checkpoint**

Create or update the project handover with branch, HEAD, test commands and results, rebuilt image ID and tool versions, Claude gate booleans, Codex regression result, remaining limitations, and the explicit statement that no push/PR/merge/release was performed. Never include credential values or transcript content.
