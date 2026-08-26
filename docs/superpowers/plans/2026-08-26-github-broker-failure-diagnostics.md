# GitHub Broker Failure Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** GitHub brokerの実host failureをcredential非露出の固定stageで診断し、1 connectionの既知failureでbroker thread全体を停止させない。

**Architecture:** 新しい小さなerror moduleで許可stageと本文を持たない`BrokerStageError`を定義する。tokenを利用するGit／PR transportは既知failureを境界ごとのstageへ変換し、connection handlerはその型だけを捕捉してsecret-free auditへ記録する。予期しないexceptionは従来どおりruntime failureとして表面化させる。

**Tech Stack:** Python 3標準ライブラリ、`unittest`、Unix domain socket、rootless Podman、Git Smart HTTP、GitHub App REST API

**Spec:** `docs/superpowers/specs/2026-08-26-github-broker-failure-diagnostics-design.md`

## Global Constraints

- token、JWT、private key、capability、Authorization header、exception本文、URL、request／response body、PR body、Git advertisement、packfile、commit内容を出力・auditしない。
- stageは`token`、`upload-discovery`、`upload-rpc`、`receive-discovery`、`receive-rpc`、`pr-request`、`response-stream`の固定集合だけを許す。
- repository、operation、permission、ref、retry、timeoutの既存allowlistを緩和しない。
- 認可前の不正requestは`denied`、認可後の既知external／protocol failureは`error`とする。
- `BaseException`または無制限な`Exception`をconnection handlerで握り潰さない。
- production変更は必ずtestを先に書き、期待理由でREDになることを確認してから行う。

---

### Task 1: Fixed Stage Error and Audit Schema

**Files:**
- Create: `src/agent_container/github_broker_error.py`
- Modify: `src/agent_container/github_broker.py`
- Test: `tests/container/test_github_broker.py`

**Interfaces:**
- Produces: `BROKER_FAILURE_STAGES: frozenset[str]`
- Produces: `BrokerStageError(stage: str)` with validated public `stage: str` and fixed secret-free message
- Extends: `BrokerSession.audit(..., stage: str | None = None)`

- [ ] **Step 1: Write failing tests for the fixed error type**

Add tests that construct every allowed stage, assert `error.stage`, assert the exception string is the same fixed text for every stage, and reject `"secret-marker"`, empty strings, and non-string values. Assert neither rejected input nor a secret marker appears in the exception text.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_github_broker.BrokerSessionTest.test_accepts_only_fixed_failure_stages
```

Expected: import or attribute failure because `BrokerStageError` does not exist.

- [ ] **Step 3: Implement the fixed error type**

Create `github_broker_error.py` with the exact allowed `frozenset`. Validate `stage` before assigning it and call `RuntimeError.__init__("GitHub broker operation failed")`; do not interpolate stage or input into the message.

- [ ] **Step 4: Write failing audit-schema tests**

Extend the broker audit test to write:

```python
session.audit(
    operation="git-upload-pack",
    status="error",
    stage="upload-rpc",
)
```

Assert the JSON record contains exactly the fixed stage. Add rejection cases for an unknown stage and for a stage supplied with `status="ok"` or `status="denied"`. Assert secret markers are absent from the audit file and raised errors.

- [ ] **Step 5: Run audit tests and verify RED**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_github_broker.BrokerSessionTest.test_audit_contains_only_allowlisted_metadata \
  tests.container.test_github_broker.BrokerSessionTest.test_audit_rejects_unvalidated_metadata_without_writing
```

Expected: `BrokerSession.audit()` rejects the new `stage` argument.

- [ ] **Step 6: Implement the audit extension**

Add `stage: str | None = None` to `BrokerSession.audit`. Accept it only when `status == "error"` and it belongs to `BROKER_FAILURE_STAGES`; reject missing stage for `error` status and reject a stage for every other status. Add only the validated stage string to the JSON record.

- [ ] **Step 7: Verify Task 1 GREEN**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.container.test_github_broker
```

Expected: all broker session tests pass, with only the existing sandbox-dependent socket skip.

### Task 2: Git Transport Stage Boundaries

**Files:**
- Modify: `src/agent_container/github_git_transport.py`
- Test: `tests/container/test_github_git_transport.py`

**Interfaces:**
- Consumes: `BrokerStageError(stage)` from Task 1
- Produces: token failures as `stage="token"`
- Produces: upload discovery/RPC and receive discovery/RPC failures with their corresponding fixed stages

- [ ] **Step 1: Write failing token-stage tests**

Add a token double whose `get()` raises `RuntimeError("secret-token-marker")`. For upload and receive discovery, assert the raised exception is `BrokerStageError`, its stage is `token`, and neither its string nor `repr` contains the marker.

- [ ] **Step 2: Run token-stage tests and verify RED**

Run the two new test methods directly. Expected: the original `RuntimeError` escapes instead of a fixed-stage error.

- [ ] **Step 3: Implement a private token boundary**

In each transport, call token retrieval through a private helper that catches only `(ValueError, RuntimeError, OSError)` from `tokens.get()` and raises `BrokerStageError("token") from None`. Preserve the one-time 401 invalidation and retry behavior.

- [ ] **Step 4: Write failing discovery/RPC stage tests**

Use `FakeResponse` and controlled `open_http` doubles to cover:

- upload discovery wrong content type -> `upload-discovery`
- upload RPC incomplete response -> `upload-rpc`
- receive discovery malformed advertisement -> `receive-discovery`
- receive RPC empty response -> `receive-rpc`

Include a secret response marker in each fixture and assert it is absent from exception string and repr.

- [ ] **Step 5: Run the four tests and verify RED**

Expected: the existing generic `ValueError` or `RuntimeError` escapes.

- [ ] **Step 6: Implement stage conversion at public transport methods**

Wrap only the known external/protocol operations in `discover()` and `rpc()`/`push()`. Re-raise an existing `BrokerStageError` unchanged so token failures remain `token`; convert other `(ValueError, RuntimeError, OSError)` into the method-specific stage with `from None`. Always close HTTP responses in the existing `finally` blocks.

- [ ] **Step 7: Verify Task 2 GREEN**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.container.test_github_git_transport
```

Expected: all Git transport tests pass.

### Task 3: PR Transport Stage Boundary

**Files:**
- Modify: `src/agent_container/github_pr.py`
- Test: `tests/container/test_github_pr.py`

**Interfaces:**
- Consumes: `BrokerStageError(stage)`
- Produces: token failures as `token`; known PR HTTP/schema failures as `pr-request`

- [ ] **Step 1: Write failing PR token and request tests**

Add one test where token retrieval raises a secret-marked error and one where GitHub returns a malformed or failing response containing a secret marker. Assert stages `token` and `pr-request` respectively, and assert no marker appears in the public error.

- [ ] **Step 2: Run the new PR tests and verify RED**

Expected: the original errors escape without fixed stages.

- [ ] **Step 3: Implement PR boundary conversion**

Wrap token retrieval separately as `token`; wrap the allowlisted PR HTTP request and response validation as `pr-request`. Preserve bounded JSON output, exact endpoint selection, 401 single retry, and absence of generic API paths.

- [ ] **Step 4: Verify Task 3 GREEN**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.container.test_github_pr
```

Expected: all PR transport tests pass.

### Task 4: Connection-Level Fail-Closed Handling

**Files:**
- Modify: `src/agent_container/github_broker_transport.py`
- Modify: `src/agent_container/github_broker_runtime.py`
- Test: `tests/container/test_github_broker_transport.py`
- Test: `tests/integration/test_github_broker_socket.py`

**Interfaces:**
- Consumes: `BrokerStageError.stage`
- Writes: `BrokerSession.audit(operation=..., status="error", stage=...)`
- Preserves: runtime-wide failure for unexpected programming errors

- [ ] **Step 1: Write failing upload discovery and RPC handler tests**

For discovery, make `transport.discover()` raise `BrokerStageError("upload-discovery")`; assert the handler returns nonzero, sends only a bounded non-secret failure frame, and audits `git-upload-pack/error/upload-discovery`.

For RPC, let discovery succeed and `transport.rpc()` raise `BrokerStageError("upload-rpc")`; assert the handler closes/fails that stream, audits `git-upload-pack/error/upload-rpc`, and does not re-raise the stage error.

- [ ] **Step 2: Run the new upload handler tests and verify RED**

Expected: discovery or RPC error escapes and no audit record exists.

- [ ] **Step 3: Implement upload connection handling**

Catch `BrokerStageError` around discovery before sending success, audit fixed stage, and return failure. Catch it again around RPC streaming, audit fixed stage, and return failure. Catch client framing/write `(ValueError, OSError)` after authorization as `response-stream`. Do not catch arbitrary `Exception`.

- [ ] **Step 4: Write and implement receive/PR equivalents with RED-GREEN cycles**

Add focused tests for `receive-discovery`, `receive-rpc`, `pr-request`, and `response-stream`. Preserve `denied` for malformed request, capability mismatch, project mismatch, repository mismatch, protected ref, delete, and disallowed PR operation.

- [ ] **Step 5: Write failing runtime-survival integration test**

Start a real broker socket with a transport that raises a known `BrokerStageError` for the first authorized connection and succeeds for the second. Assert the second connection is accepted and the runtime exits without `GitHubBrokerRuntimeError`.

- [ ] **Step 6: Run the integration test and verify RED**

Expected: the broker thread stops after the first connection or the runtime reports failure.

- [ ] **Step 7: Implement connection isolation in the runtime loop**

Keep `handle_broker_connection` responsible for consuming known stage errors. In `_serve`, continue accepting after a handler returns a nonzero result. Preserve the existing outer `BaseException` capture only for unexpected listener/thread failures.

- [ ] **Step 8: Verify Task 4 GREEN**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_github_broker_transport \
  tests.integration.test_github_broker_socket
```

Expected: all tests pass, with only environment-dependent socket skips.

### Task 5: Documentation, Full Verification, and Live Diagnosis

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `docs/phase3-github-broker.md`
- Modify after observation: `docs/phase3-github-broker-smoke-test.md`

**Interfaces:**
- Consumes: secret-free audit record with fixed `stage`
- Produces: operator procedure that never displays audit bodies outside allowlisted fields

- [ ] **Step 1: Document the diagnostic contract**

Add the fixed stage list, `error` semantics, connection-level fail-closed behavior, and a `jq` command that selects only `timestamp`, `operation`, `status`, and `stage`. State explicitly that raw response bodies, exception bodies, tokens, and Git payloads must not be collected.

- [ ] **Step 2: Update CHANGELOG**

Record connection-level secret-free failure classification and the fact that known connection failures no longer terminate the broker accept loop.

- [ ] **Step 3: Run fresh full verification**

Run:

```bash
PYTHONPATH=src python3 -m unittest
git diff --check
```

Expected: 0 failures and no whitespace errors. Record the exact test and skip counts from fresh output.

- [ ] **Step 4: Review the complete diff against the spec**

Verify every fixed stage has a test, unknown stages are rejected, secret markers are absent, external allowlists are unchanged, and no broad exception handler was added to a connection handler.

- [ ] **Step 5: Re-run the host broker project registration**

Run the updated workspace launcher for `ghb-smoke`. If clone fails, inspect only the allowlisted latest audit fields:

```bash
jq -c '{timestamp,operation,status,stage}' \
  "$AGENT_CONTAINER_HOME/github-broker/audit/events.jsonl" | tail -n 5
```

Do not print raw audit lines or GitHub error bodies.

- [ ] **Step 6: Continue from the observed fixed stage**

If registration succeeds, run both broker doctors and continue the Phase 3 credential non-exposure, exact-repository denial, fail-closed, push, and PR checklist. If registration fails, return to systematic debugging scoped only to the recorded stage; do not modify another component without a new failing test.

- [ ] **Step 7: Commit from the host checkout after live verification**

Because `.git` is read-only inside the current container, review and stage only the intended files from the host-side state workspace. Exclude `.claude/` and any runtime state. Use a focused commit message such as:

```bash
git commit -m "fix: classify GitHub broker connection failures"
```
