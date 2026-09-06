# Claude strong nested sandbox implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch the Claude runtime's nested sandbox from weaker mode to strong mode (`enableWeakerNestedSandbox: false`) and make the Claude launcher force `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`, with managed policy, launcher, and docs/tests updated and pinned to match.

**Architecture:** Two source files change (`profiles/claude/managed-settings.json`'s `sandbox.enableWeakerNestedSandbox`, `src/agent_container/claude_policy.py`'s `EXPECTED_SETTINGS`, and `src/agent_container/claude_launcher.py`'s `exec_claude`); everything else (outer Podman argv, `/proc` unmask, all other managed-policy keys) is unchanged. A new real-Podman integration test proves the runtime can start a `bwrap --unshare-pid --proc /proc` sandbox with the same argv prefix `run_claude_spec` produces. Docs and fixed test-count pins are updated to match.

**Tech Stack:** Python 3, `unittest`, Podman/bubblewrap, GitHub Actions CI.

**Spec:** `docs/superpowers/specs/2026-09-06-claude-strong-nested-sandbox-design.md`

## Global Constraints

- No fallback to weaker mode. `sandbox.enableWeakerNestedSandbox` becomes `false` unconditionally; there is no project/env toggle.
- `failIfUnavailable: true` stays `true`. If sandbox startup fails, Claude must fail to start, never fall back to unsandboxed or weaker mode.
- Every other managed-settings key (`enabled`, `allowUnsandboxedCommands: false`, `network.allowAllUnixSockets: true`, `credentials` deny lists, `permissions.deny`, `disableBypassPermissionsMode`, `allowManagedHooksOnly`, `allowedMcpServers: []`, `allowManagedMcpServersOnly`, `statusLine`) is unchanged.
- Outer Podman constraints (`--read-only`, `--cap-drop=all`, `no-new-privileges`, keep-id, mount layout, the `/proc` unmask from PR #108) are unchanged.
- CI's Podman gate fixed count goes from 16 to 17 (one new real-Podman test method, no new module).
- The live/host Security acceptance gate (private terminal TUI checks) is explicitly out of scope for this plan — it happens after merge and rebuild, run by the user directly.

---

### Task 1: Flip `enableWeakerNestedSandbox` to `false` in managed policy

**Files:**
- Modify: `profiles/claude/managed-settings.json:4`
- Modify: `src/agent_container/claude_policy.py:12`
- Modify: `tests/container/test_claude_policy.py:192`
- Modify: `tests/container/test_image.py:165`
- Test: `tests/container/test_claude_policy.py`, `tests/container/test_image.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `profiles/claude/managed-settings.json` and `agent_container.claude_policy.EXPECTED_SETTINGS` both carry `sandbox.enableWeakerNestedSandbox = False`. Later tasks (3, 6) reference this fact in docs but do not call any new function.

- [ ] **Step 1: Edit the two tests first (RED)**

In `tests/container/test_claude_policy.py`, the mutation-table entry currently reads (line 192):

```python
            "strong nested": lambda value: value["sandbox"].update(enableWeakerNestedSandbox=False),
```

Replace it with (this now tests that *re-enabling* weaker mode is rejected, since the baseline will be `false` after Step 3):

```python
            "weak nested": lambda value: value["sandbox"].update(enableWeakerNestedSandbox=True),
```

In `tests/container/test_image.py`, the assertion currently reads (line 165):

```python
        self.assertTrue(settings["sandbox"]["enableWeakerNestedSandbox"])
```

Replace it with:

```python
        self.assertFalse(settings["sandbox"]["enableWeakerNestedSandbox"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_claude_policy tests.container.test_image -v`

Expected: FAIL. `test_rejects_every_security_critical_mutation_without_output` fails on the `weak nested` subTest (mutating an already-`true` baseline to `true` is a no-op, so `validate_managed_policy` still returns `True`, and `assertFalse(valid)` fails). `test_image_copies_exact_claude_managed_policy` fails because `assertFalse(True)` fails.

- [ ] **Step 3: Flip the two source files (GREEN)**

In `profiles/claude/managed-settings.json` line 4, change:

```json
    "enableWeakerNestedSandbox": true,
```

to:

```json
    "enableWeakerNestedSandbox": false,
```

In `src/agent_container/claude_policy.py` line 12, change:

```python
        "enableWeakerNestedSandbox": True,
```

to:

```python
        "enableWeakerNestedSandbox": False,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_claude_policy tests.container.test_image -v`

Expected: PASS, all tests including `test_accepts_repository_managed_policy` (which reads the repository file directly and compares against `EXPECTED_SETTINGS`, so it passes once both sides agree on `false`).

- [ ] **Step 5: Commit**

```bash
git add profiles/claude/managed-settings.json src/agent_container/claude_policy.py tests/container/test_claude_policy.py tests/container/test_image.py
git commit -m "feat: switch Claude managed policy to strong nested sandbox

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01X8GKARy64XhjPsDhKqvm7R"
```

---

### Task 2: Launcher forces `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`

**Files:**
- Modify: `src/agent_container/claude_launcher.py:130-131`
- Modify: `tests/container/test_claude_launcher.py:126-151`
- Test: `tests/container/test_claude_launcher.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `exec_claude` always sets `environment["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] = "1"` before `execvpe`, regardless of the caller's environment. Task 3's docs describe this behavior; no other task calls it directly.

- [ ] **Step 1: Rewrite the test first (RED)**

In `tests/container/test_claude_launcher.py`, replace the whole `test_exec_claude_sets_parent_token_without_global_scrub` method (lines 126-151):

```python
    def test_exec_claude_sets_parent_token_without_global_scrub(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = self.write_token(Path(temporary))
            observed: dict[str, object] = {}

            def fake_execvpe(program, argv, environment):
                observed["has_token"] = "CLAUDE_CODE_OAUTH_TOKEN" in environment
                observed["has_scrub"] = (
                    "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB" in environment
                )
                raise ExecObserved

            with patch.dict(
                os.environ,
                {
                    "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
                    "CLAUDE_CONFIG_DIR": temporary,
                },
            ):
                with self.assertRaises(ExecObserved):
                    exec_claude(token_file, ("claude",), fake_execvpe)

            self.assertEqual(
                observed,
                {"has_token": True, "has_scrub": False},
            )
```

with:

```python
    def test_exec_claude_sets_parent_token_and_global_scrub(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = self.write_token(Path(temporary))
            observed: dict[str, object] = {}

            def fake_execvpe(program, argv, environment):
                observed["has_token"] = "CLAUDE_CODE_OAUTH_TOKEN" in environment
                observed["scrub"] = environment.get(
                    "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"
                )
                raise ExecObserved

            with patch.dict(
                os.environ,
                {
                    "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "0",
                    "CLAUDE_CONFIG_DIR": temporary,
                },
            ):
                with self.assertRaises(ExecObserved):
                    exec_claude(token_file, ("claude",), fake_execvpe)

            self.assertEqual(
                observed,
                {"has_token": True, "scrub": "1"},
            )
```

(This mirrors the existing `test_exec_claude_forces_demo_mode_over_caller_environment` pattern: the caller sets a different value, `"0"`, and the launcher must force it to `"1"` regardless.)

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_claude_launcher.ClaudeLauncherTest.test_exec_claude_sets_parent_token_and_global_scrub -v`

Expected: FAIL — `observed` is `{"has_token": True, "scrub": None}` because the current code does `environment.pop("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", None)`, not `{"has_token": True, "scrub": "1"}`.

- [ ] **Step 3: Implement the minimal change**

In `src/agent_container/claude_launcher.py`, in `exec_claude` (currently lines 130-131):

```python
    environment = os.environ.copy()
    environment.pop("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", None)
```

Replace with:

```python
    environment = os.environ.copy()
    environment["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] = "1"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_claude_launcher -v`

Expected: PASS, full file (31 tests), including the rewritten test and the unrelated `test_exec_claude_forces_demo_mode_over_caller_environment`.

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/claude_launcher.py tests/container/test_claude_launcher.py
git commit -m "feat: force CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 in the Claude launcher

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01X8GKARy64XhjPsDhKqvm7R"
```

---

### Task 3: Update operator/smoke docs for strong mode and pin the wording

**Files:**
- Modify: `docs/phase2-claude-code.md:209,215,217`
- Modify: `docs/phase2-smoke-test.md:93,101-109`
- Modify: `tests/container/test_docs.py` (`test_operator_docs_define_final_nested_claude_constraints`, `test_smoke_guide_contains_claude_sandbox_security_gate`)
- Test: `tests/container/test_docs.py`

**Interfaces:**
- Consumes: nothing (independent of Tasks 1-2 at the text level, though it documents their effect).
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Update the pinning tests first (RED)**

In `tests/container/test_docs.py`, find `test_operator_docs_define_final_nested_claude_constraints`. Replace the line:

```python
            "global scrubは意図的に設定しません",
```

with:

```python
            "global scrubとして意図的に設定します",
```

and add one guard line right after the `for expected in (...): self.assertIn(...)` loop in that same method (after the existing `self.assertIn("Claudeのmanaged sandbox", codex)` / `self.assertIn("Codexのhook設定とは別", codex)` lines), so the full tail of the method reads:

```python
        self.assertIn("Claudeのmanaged sandbox", codex)
        self.assertIn("Codexのhook設定とは別", codex)
        self.assertNotIn("global scrubは意図的に設定しません", phase2)
```

Next, find `test_smoke_guide_contains_claude_sandbox_security_gate`. Add two entries to its `for expected in (...)` tuple (anywhere in the tuple; appending at the end is simplest) and one guard assertion after the existing `self.assertIn("即座に停止", body)` line:

```python
            "enableWeakerNestedSandboxが無効",
            "親Claude processのPIDが見える",
```

```python
        self.assertIn("即座に停止", body)
        self.assertNotIn("enableWeakerNestedSandboxが有効", body)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_docs.Phase2DocumentationTest.test_operator_docs_define_final_nested_claude_constraints tests.container.test_docs.Phase2DocumentationTest.test_smoke_guide_contains_claude_sandbox_security_gate -v`

Expected: FAIL. `test_operator_docs_define_final_nested_claude_constraints` fails because `docs/phase2-claude-code.md` still contains the literal string `global scrubは意図的に設定しません` and does not yet contain `global scrubとして意図的に設定します`. `test_smoke_guide_contains_claude_sandbox_security_gate` fails because `docs/phase2-smoke-test.md` does not yet contain `enableWeakerNestedSandboxが無効` or `親Claude processのPIDが見える`, and still contains `enableWeakerNestedSandboxが有効`.

- [ ] **Step 3: Edit `docs/phase2-claude-code.md`**

Replace the paragraph at line 209:

```
専用launcherだけがsecret fileを`O_NOFOLLOW`で開いてtokenをClaude親processへ渡します。tokenはPodman argvにもhost環境にも入りません。現在のClaude Codeでは`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`がrootless Podman内で利用できない強いsandboxを強制し、新しい`/proc`のmountに失敗するため、global scrubは意図的に設定しません。この失敗はagent runtimeの`/proc` unmaskと同じ原因の可能性がありますが、強いsandboxとglobal scrubの再評価は未実施で、本書の設定は変えていません。
```

with:

```
専用launcherだけがsecret fileを`O_NOFOLLOW`で開いてtokenをClaude親processへ渡します。tokenはPodman argvにもhost環境にも入りません。launcherは`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`をglobal scrubとして意図的に設定します。Claude Code文書によれば、この変数を設定すると全subprocessからcredentialが除去され、Linuxでは強いsandboxを強制します。agent runtimeの`--security-opt=unmask=/proc/*`（`/proc` unmask）により、strong modeが必要とする新しい`/proc`のuser namespace内mountが成功するため、2026-08-24設計がglobal scrub採用を見送った前提は解消しました。
```

Replace the paragraph at line 215:

```
代わりに、image内のEnterprise managed settingsでweaker nested sandboxを有効にし、unsandboxed fallbackを禁止します。credential環境変数とtoken fileへのsubprocess access、built-in Readによるtoken path参照を拒否し、hooksとMCPは初期状態で無効にします。review済みHTTP MCPは将来managed policyへ追加できますが、stdio MCPはglobal scrubとnested modeが安全に共存できるまで無効のままです。
```

with:

```
image内のEnterprise managed settingsで`enableWeakerNestedSandbox`を`false`にし、strong nested sandboxを使います。unsandboxed fallbackは禁止します。credential環境変数とtoken fileへのsubprocess access、built-in Readによるtoken path参照を拒否し、hooksとMCPは初期状態で無効にします。review済みHTTP MCPは将来managed policyへ追加できますが、stdio MCPは無効のままです。
```

Replace the paragraph at line 217:

```
外側のPodman制約である`--read-only`、`--cap-drop=all`、`no-new-privileges`、keep-id、狭いmount、container PID namespace、bounded tmpfsは変更しません。weaker nested modeではClaude親processが既存`/proc`に見える可能性があるため、実hostでは専用probeの`parent_token_via_proc_readable=false`を必須にします。`parent_token_via_proc_readable=true`、token fileを読める、またはBash環境にtokenが見える場合は運用を停止し、sandboxを無効化して再試行しません。確認時も環境一覧、値、prefix、長さ、hash、process environment、secret fileや`/proc/*/environ`の本文を表示しません。
```

with:

```
外側のPodman制約である`--read-only`、`--cap-drop=all`、`no-new-privileges`、keep-id、狭いmount、container PID namespace、bounded tmpfsは変更しません。strong nested modeでは新しいPID namespaceとprocfsにより親Claude processが構造的に見えなくなりますが、念のため実hostでは専用probeの`parent_token_via_proc_readable=false`を必須にします。`parent_token_via_proc_readable=true`、token fileを読める、またはBash環境にtokenが見える場合は運用を停止し、sandboxを無効化して再試行しません。確認時も環境一覧、値、prefix、長さ、hash、process environment、secret fileや`/proc/*/environ`の本文を表示しません。
```

- [ ] **Step 4: Edit `docs/phase2-smoke-test.md`**

Replace step 3 (line 93):

```
3. Claude内で`/status`を確認した後、`/sandbox`の`Config`を開く。managed settingsが読み込まれ、sandboxが有効、`enableWeakerNestedSandbox`が有効、unsandboxed fallbackが禁止されていることを確認する。続けて`/hooks`と`/mcp`を開き、hookとMCP serverがどちらも空であることを確認する。managed policyを確認できない、sandboxが無効、fallback可能、hookまたはMCPが1件でも読み込まれている場合は即座に停止し、sandboxを無効化して再試行しない。
```

with:

```
3. Claude内で`/status`を確認した後、`/sandbox`の`Config`を開く。managed settingsが読み込まれ、sandboxが有効、`enableWeakerNestedSandbox`が無効（strong nested sandbox）、unsandboxed fallbackが禁止されていることを確認する。続けて`/hooks`と`/mcp`を開き、hookとMCP serverがどちらも空であることを確認する。managed policyを確認できない、sandboxが無効、fallback可能、hookまたはMCPが1件でも読み込まれている場合は即座に停止し、sandboxを無効化して再試行しない。
```

Replace the paragraph that follows the 3-line probe output block (line 109):

```
   いずれかが`true`、commandがsandbox内で実行不能、または3行以外のcredential由来情報が出た場合は即座に停止する。特に`parent_token_via_proc_readable=true`ならこの方式を不採用とし、Phase 2完了を宣言しない。確認時はcredentialの値、長さ、prefix、hash、環境一覧、環境entry、process environment、`/proc/*/environ`の内容、`/run/secrets/claude-oauth-token`本文を表示・記録しない。containerの`--read-only`、`--cap-drop=all`、`no-new-privileges`、keep-id、tmpfsも弱めない。
```

with:

```
   いずれかが`true`、commandがsandbox内で実行不能、または3行以外のcredential由来情報が出た場合は即座に停止する。特に`parent_token_via_proc_readable=true`ならこの方式を不採用とし、Phase 2完了を宣言しない。続けて同じBash toolで`ls /proc | grep -c '^[0-9]\+$'`を実行し、出力がsandbox内process（probeと実行中shellなど）の数と一致し、container内の他process（親Claude processを含む）のPIDを含まないことを確認する。数が合わない、または親Claude processのPIDが見える場合は停止し、sandboxを無効化して再試行しない。確認時はcredentialの値、長さ、prefix、hash、環境一覧、環境entry、process environment、`/proc/*/environ`の内容、`/run/secrets/claude-oauth-token`本文を表示・記録しない。containerの`--read-only`、`--cap-drop=all`、`no-new-privileges`、keep-id、tmpfsも弱めない。
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_docs -v`

Expected: PASS, full file (all tests, no unexpected skips).

- [ ] **Step 6: Commit**

```bash
git add docs/phase2-claude-code.md docs/phase2-smoke-test.md tests/container/test_docs.py
git commit -m "docs: describe strong nested sandbox instead of weaker-mode caveats

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01X8GKARy64XhjPsDhKqvm7R"
```

---

### Task 4: Add a real-Podman integration test proving strong-mode argv works

**Files:**
- Modify: `tests/integration/test_agent_sandbox_podman.py`
- Test: `tests/integration/test_agent_sandbox_podman.py` (opt-in, real Podman)

**Interfaces:**
- Consumes: `run_claude_spec` from `agent_container.podman` (existing signature: `run_claude_spec(layout, handover_project, image, uid, gid, handover_broker, broker=None, egress=None, family_mount=None) -> CommandSpec`), `HandoverRuntimeMount` from `agent_container.handover_broker_runtime` (existing: `HandoverRuntimeMount(run_dir: Path)`), `StateLayout` from `agent_container.state` (existing).
- Produces: nothing consumed by later tasks — this is a leaf test file.

This test does not depend on Task 1's or Task 2's changes: it builds a `CommandSpec` via `run_claude_spec` and replaces the agent command with a raw `bwrap` probe, so it only requires the base image to already have `bwrap` and the `/proc` unmask from PR #108 (already on `main`). It can be validated against the existing `localhost/agent-container:dev` image without rebuilding.

- [ ] **Step 1: Write the new test**

In `tests/integration/test_agent_sandbox_podman.py`, add the following import alongside the existing ones at the top of the file:

```python
from agent_container.handover_broker_runtime import HandoverRuntimeMount
from agent_container.podman import run_claude_spec
```

(`run_codex_spec` import stays as-is.)

Add the following method to `AgentSandboxPodmanIntegrationTest`, after `test_codex_workspace_write_sandbox_runs_a_command` and before the `_runtime_state` static method:

```python
    def test_claude_strong_nested_sandbox_runs_a_command(self) -> None:
        token = f"AGENT_SANDBOX_{secrets.token_hex(8)}"
        with tempfile.TemporaryDirectory(
            prefix="agent-container-claude-sandbox-"
        ) as temporary:
            layout, handover_project, handover_broker = self._claude_runtime_state(
                Path(temporary)
            )
            spec = run_claude_spec(
                layout,
                handover_project,
                BASE_IMAGE,
                os.getuid(),
                os.getgid(),
                handover_broker,
            )
            argv = [
                argument
                for argument in spec.argv
                if argument not in {"--interactive", "--tty"}
            ]
            agent_start = argv.index("python3", argv.index(BASE_IMAGE))
            command = (
                *argv[:agent_start],
                "bwrap",
                "--unshare-user",
                "--unshare-pid",
                "--proc",
                "/proc",
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "/usr/bin/printf",
                token,
            )

            completed = subprocess.run(
                command, check=False, capture_output=True, text=True, timeout=180
            )

        self.assertEqual(
            completed.returncode,
            0,
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
        )
        self.assertEqual(completed.stdout.strip(), token)
        self.assertNotIn("Can't mount proc", completed.stderr)
```

Add the following static method next to `_runtime_state`:

```python
    @staticmethod
    def _claude_runtime_state(
        root: Path,
    ) -> tuple[StateLayout, Path, HandoverRuntimeMount]:
        state = root / "state"
        handovers = root / "handovers"
        for directory in (
            state,
            state / "gh",
            state / "shared-auth/claude",
            state / "projects/agent-container/claude-config",
            state / "projects/agent-container/cache",
            state / "workspaces/agent-container/.git",
            state / "handover-broker/one",
            handovers / "agent-container",
        ):
            directory.mkdir(mode=0o700, parents=True)
        token_file = state / "shared-auth/claude/oauth-token"
        token_file.write_text("sk-ant-oat01-test\n", encoding="utf-8")
        token_file.chmod(0o600)
        layout = StateLayout(state.resolve(), "agent-container")
        handover_broker = HandoverRuntimeMount(
            (state / "handover-broker/one").resolve()
        )
        return layout, (handovers / "agent-container").resolve(), handover_broker
```

- [ ] **Step 2: Run the test against the existing dev image**

Run: `AGENT_CONTAINER_RUN_PODMAN_INTEGRATION=1 PYTHONPATH=src python3 -m unittest tests.integration.test_agent_sandbox_podman -v`

Expected: PASS, 2 tests (the existing Codex test plus the new Claude test), 0 skipped. If it fails with `Can't mount proc`, the local `localhost/agent-container:dev` image predates PR #108's `/proc` unmask — rebuild with `bin/agentctl build` before retrying; do not weaken the bwrap flags to work around it.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_agent_sandbox_podman.py
git commit -m "test: add real-Podman evidence for the Claude strong nested sandbox argv

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01X8GKARy64XhjPsDhKqvm7R"
```

---

### Task 5: Bump the Podman gate fixed count from 16 to 17

**Files:**
- Modify: `.github/workflows/ci.yml:200`
- Modify: `tests/container/test_docs.py:315,884`
- Modify: `docs/family-issue-create-broker-smoke-test.md:29,40`
- Modify: `docs/superpowers/specs/2026-09-04-broker-kernel-design.md:145`
- Test: `tests/container/test_docs.py`

**Interfaces:**
- Consumes: Task 4's new test (the count bump reflects it existing).
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Update the pinning assertions first (RED)**

In `tests/container/test_docs.py`, `test_ci_requires_complete_family_podman_gate_without_skips` (around line 315), change:

```python
            'grep -F "Ran 16 tests" "$podman_log"',
```

to:

```python
            'grep -F "Ran 17 tests" "$podman_log"',
```

In the same file, `test_smoke_commands_use_exact_opt_ins_and_local_unknown_fixture` (around line 884), change:

```python
            "Podman suiteは`Ran 16 tests ... OK`",
```

to:

```python
            "Podman suiteは`Ran 17 tests ... OK`",
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_docs.EgressDocumentationTest.test_ci_requires_complete_family_podman_gate_without_skips tests.container.test_docs.FamilyIssueBrokerDocumentationTest.test_smoke_commands_use_exact_opt_ins_and_local_unknown_fixture -v`

Expected: FAIL on both tests above — `.github/workflows/ci.yml` still says `Ran 16 tests`, and `docs/family-issue-create-broker-smoke-test.md` still says `Ran 16 tests ... OK`.

- [ ] **Step 3: Update CI and the two docs files**

In `.github/workflows/ci.yml` line 200, change:

```
          grep -F "Ran 16 tests" "$podman_log"
```

to:

```
          grep -F "Ran 17 tests" "$podman_log"
```

In `docs/family-issue-create-broker-smoke-test.md` line 29, append this sentence to the end of the existing paragraph (after the sentence ending "...container期待件数を1125へ更新しました。"):

```
Claude strong nested sandboxの独立修正（real Podman test 1件を追加）を取り込んだ基準では、Podman suite期待件数を17へ更新しました。
```

In the same file, line 40, change:

```
固定期待値はPodman suiteは`Ran 16 tests ... OK`かつunexpected skip 0件です。
```

to:

```
固定期待値はPodman suiteは`Ran 17 tests ... OK`かつunexpected skip 0件です。
```

In `docs/superpowers/specs/2026-09-04-broker-kernel-design.md` line 145, append this sentence to the end of the existing paragraph (after "...container期待値を1125とします。"):

```
Claude strong nested sandboxの独立修正（real Podman test 1件を追加）を取り込んだ基準では、real Podman suite期待値を17とします。
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_docs -v`

Expected: PASS, full file.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml tests/container/test_docs.py docs/family-issue-create-broker-smoke-test.md docs/superpowers/specs/2026-09-04-broker-kernel-design.md
git commit -m "docs: bump the fixed Podman gate count to 17 for the new sandbox test

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01X8GKARy64XhjPsDhKqvm7R"
```

---

### Task 6: Record the design change in README, CHANGELOG, and the 2026-08-24 design doc

**Files:**
- Modify: `README.md:352`
- Modify: `CHANGELOG.md` (Unreleased section)
- Modify: `docs/superpowers/specs/2026-08-24-debian-project-images-claude-sandbox-design.md` (`## Existing designとの関係` section)

**Interfaces:**
- Consumes: nothing new (references facts established by Tasks 1-2).
- Produces: nothing consumed by later tasks.

This task is documentation-only; there is no test to write first (no test in this repo pins README/CHANGELOG prose, and `test_readme_and_changelog_advertise_shipped_scope_without_stale_claim` only checks unrelated strings — verify this by re-running `tests.container.test_docs` after editing, per Step 3 below).

- [ ] **Step 1: Edit `README.md`**

Find the paragraph at line 352 (starts with "runtimeはrootless Podman..."). Append this sentence to the end of it:

```
Claudeのnested sandboxはstrong mode（`enableWeakerNestedSandbox=false`）で動き、launcherが`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`を設定して全subprocessからcredentialを除去します。
```

- [ ] **Step 2: Edit `CHANGELOG.md`**

In the `## [Unreleased]` section, insert a new `### Changed` section immediately after the existing `### Fixed` section and before `### Security boundaries`:

```markdown
### Changed

- Claude runtimeのnested sandboxをstrong mode（`sandbox.enableWeakerNestedSandbox=false`）に切り替え、launcherが`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`を設定するようにしました。PR #108の`/proc` unmaskによりstrong modeが必要とする新しい`/proc`のuser namespace内mountが可能になったため、2026-08-24設計がglobal scrub採用を見送った前提が解消しました（[設計](superpowers/specs/2026-09-06-claude-strong-nested-sandbox-design.md)）。fallbackは設けず、`failIfUnavailable: true`は維持します。
```

In the same `## [Unreleased]` section, append this bullet to the end of the existing `### Security boundaries` section:

```markdown
- Claude sandbox内から見える`/proc`が、strong modeの新しいPID namespaceにより構造的にsandbox内processだけへ縮小されます。従来のweaker modeでは親Claude processを含むcontainer内の他processが構造的には見えており、防御は専用probeの`parent_token_via_proc_readable=false`という観測だけに依存していました。global scrubにより、Bash toolを含む全subprocessからcredential環境変数が除去されます。既存のcredential deny list、token file deny、built-in Read deny、`allowAllUnixSockets`、`failIfUnavailable`、unsandboxed command禁止、hooks／MCPのmanaged限定は変更しません。
```

- [ ] **Step 3: Edit `docs/superpowers/specs/2026-08-24-debian-project-images-claude-sandbox-design.md`**

In the `## Existing designとの関係` section, append this sentence to the end of the existing paragraph (after "...競合しない差分として修正する。"):

```
2026-09-06、agent runtimeの`/proc` unmask（PR #108）によりoption 2の不採用理由が解消したため、[2026-09-06設計](2026-09-06-claude-strong-nested-sandbox-design.md)でoption 2（strong nested sandbox + global scrub）を採用した。
```

- [ ] **Step 4: Run the full docs test file to confirm nothing broke**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_docs -v`

Expected: PASS, full file, no unexpected skips.

- [ ] **Step 5: Commit**

```bash
git add README.md CHANGELOG.md docs/superpowers/specs/2026-08-24-debian-project-images-claude-sandbox-design.md
git commit -m "docs: record the strong nested sandbox change in README and CHANGELOG

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01X8GKARy64XhjPsDhKqvm7R"
```

---

### Task 7: Full verification pass

**Files:** none (verification only).

**Interfaces:**
- Consumes: everything from Tasks 1-6.
- Produces: confidence the branch is ready for a PR.

- [ ] **Step 1: Run the full container unit suite**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests/container -v 2>&1 | tail -5`

Expected: `OK`, no unexpected skips. Note the exact `Ran N tests` count for the PR description (it should equal the pre-existing 1125, since this plan added zero container unit tests — only modified/renamed existing ones).

- [ ] **Step 2: Run the Codex unit suite (should be untouched)**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests/codex -v 2>&1 | tail -5`

Expected: `OK`, `Ran 49 tests`, no unexpected skips (unchanged by this plan).

- [ ] **Step 3: Run lint and whitespace checks**

Run: `bin/lint`

Expected: exit 0, no output.

Run: `git diff --check`

Expected: exit 0, no output.

- [ ] **Step 4: Run the real Podman integration suite**

Run: `AGENT_CONTAINER_RUN_PODMAN_INTEGRATION=1 PYTHONPATH=src python3 -m unittest tests.integration.test_project_image_podman tests.integration.test_egress_podman tests.integration.test_family_intake_podman tests.integration.test_agent_sandbox_podman tests.integration.test_codex_sandbox_network_podman -v 2>&1 | tail -5`

Expected: `OK`, `Ran 17 tests`, no skipped tests. If `AGENT_FAMILY_TEST_IMAGE`/`localhost/agent-family-test:local` prerequisites are missing for `test_family_intake_podman`, record `not run — missing prerequisite` for that module specifically rather than skipping silently, per this repo's fail-closed convention; the new Claude sandbox test only needs `localhost/agent-container:dev` (or `AGENT_CONTAINER_INTEGRATION_BASE_IMAGE`) and does not need the Family test image.

- [ ] **Step 5: Review the full diff against the spec's Completion criteria**

Run: `git log --oneline main..HEAD` and `git diff main...HEAD --stat`

Confirm every item in the spec's `## Completion criteria` list 1 (CI, unit, Podman 17) and 3 (docs no longer describe weaker-mode assumptions) is satisfied by the diff. Criterion 2 (live host gate) is explicitly out of scope for this plan.

- [ ] **Step 6: Report results, do not push or open a PR without asking**

Summarize commit list, test counts, and lint/diff-check results back to the user before pushing `feat/claude-strong-nested-sandbox` or opening a PR — pushing and PR creation are visible/hard-to-reverse actions the user should confirm first, per this project's working agreement.
