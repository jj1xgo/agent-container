# Family Issue Create Broker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a runtime submit one bounded family-repository Issue draft while ensuring that only a host operator's per-request interactive approval can create the Issue.

**Architecture:** A credential-free Unix-socket intake plane validates and persists immutable canonical requests in project-scoped private state. A separately invoked host approval plane owns a distinct GitHub App, revalidates repository name and ID, drives an explicit lifecycle including ambiguous outcomes, and emits content-free audit events. Existing Codex and Claude launch paths only mount the intake socket and one-time capability; credentials and approval commands remain host-only.

**Tech Stack:** Python 3.11+ standard library, Unix domain sockets, rootless Podman 5.8+, GitHub REST API 2026-03-10, `unittest`, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-30-family-issue-create-broker-design.md`

## Global Constraints

- Initial scope is Issue creation only: no comments, edits, close/reopen, labels, assignees, milestones, projects, attachments, discussions, sub-issues, generic REST, or GraphQL.
- Bind each project to exactly one normalized `owner/name` and positive repository ID; runtime requests never select a repository.
- Family App, key, installation, metadata, state, socket, policy, audit, and lifecycle remain separate from the development GitHub broker.
- The intake process has no GitHub credential or outbound GitHub client; the container receives no family repository identity, credential, token, raw response, or host path.
- Request limits are title 256 UTF-8 bytes, summary 2 KiB, context 4 KiB, 1–20 acceptance criteria of at most 512 bytes each, and a 16 KiB frame.
- Reject NUL, C0/C1 controls, bidi overrides, invalid newlines, unknown/missing/duplicate fields, unsafe filesystem objects, and owner/mode violations before external communication.
- Pending content lives for 24 hours in `0700` directories and `0600` regular files; at most 10 unfinished requests exist per project and one request is accepted per runtime run.
- GitHub send ambiguity transitions to `unknown`; never automatically retry, reapprove, or infer whether creation occurred.
- Audit records contain only timestamp, project ID, request ID, fixed operation/status, and fixed failure stage.
- Every behavior change is test-first. Do not claim Unix-socket, Podman, or real-host PASS without evidence from an environment that can run it.

---

### Task 1: Family state layout, binding, and secure persistence

**Files:**
- Modify: `src/agent_container/state.py`
- Create: `src/agent_container/family_state.py`
- Modify: `tests/container/test_state.py`
- Create: `tests/container/test_family_state.py`

**Interfaces:**
- Produces: `StateLayout.family_root`, `.family_app_file`, `.family_private_key_file`, `.family_project_dir`, `.family_binding_file`, `.family_pending_dir`, `.family_audit_file`, and `.family_intake_run_root`.
- Produces: `FamilyBinding(repository: Repository, repository_id: int)` and `load_family_binding(path: Path) -> FamilyBinding`.
- Produces: `write_family_binding(path: Path, binding: FamilyBinding) -> None` with exclusive creation and durable same-directory replacement only for an explicitly observed existing binding.
- Consumes: `Repository.parse`, `validate_repository_id`, `ensure_private_directory`, and `ensure_private_file`.

- [ ] **Step 1: Write failing layout, schema, and filesystem tests**

Add exact path assertions and table-driven rejection tests:

```python
layout = StateLayout(Path("/state"), "demo")
self.assertEqual(layout.family_app_file, Path("/state/family/app.json"))
self.assertEqual(layout.family_binding_file,
                 Path("/state/family/projects/demo/binding.json"))
self.assertEqual(layout.family_pending_dir,
                 Path("/state/family/projects/demo/pending"))
self.assertEqual(
    load_family_binding(binding_path),
    FamilyBinding(Repository("family", "roadmap"), 12345),
)
```

Cover duplicate/extra/missing JSON keys, bool/zero/negative repository IDs, malformed UTF-8/JSON, non-normalized repositories, symlinked ancestors/files, FIFO, hard-link count other than one, wrong owner/mode, changed inode during read/write, collision, partial write, file/parent `fsync` failure, and preservation of unrelated siblings.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_state tests.container.test_family_state -v`

Expected: FAIL because family layout and state APIs do not exist.

- [ ] **Step 3: Implement the exact binding schema and descriptor-relative private I/O**

Use an immutable schema and duplicate-rejecting decoder:

```python
@dataclass(frozen=True)
class FamilyBinding:
    repository: Repository
    repository_id: int

def load_family_binding(path: Path) -> FamilyBinding:
    payload = _read_private_json(path, maximum_bytes=4096)
    if set(payload) != {"repository", "repository_id"}:
        raise ValueError("family binding is invalid")
    return FamilyBinding(
        Repository.parse(_exact_text(payload["repository"])),
        validate_repository_id(payload["repository_id"]),
    )
```

Open every ancestor with `O_DIRECTORY|O_NOFOLLOW`, files with `O_NOFOLLOW`, compare `lstat`/`fstat` device and inode, require current UID, `0700`/`0600`, regular type, and link count one. Use `O_CREAT|O_EXCL`, complete writes, file `fsync`, atomic rename, and parent `fsync`.

- [ ] **Step 4: Run focused tests and lint**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.container.test_state tests.container.test_family_state -v
bin/lint
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/agent_container/state.py src/agent_container/family_state.py \
  tests/container/test_state.py tests/container/test_family_state.py
git commit -m "feat: add private family repository state"
```

---

### Task 2: Fixed request schema and canonical Markdown

**Files:**
- Create: `src/agent_container/family_issue.py`
- Create: `tests/container/test_family_issue.py`

**Interfaces:**
- Produces: `FamilyIssueDraft(title: str, summary: str, context: str, acceptance_criteria: tuple[str, ...])`.
- Produces: `parse_family_issue_draft(payload: object) -> FamilyIssueDraft` and `render_family_issue_body(draft: FamilyIssueDraft) -> str`.
- Produces: `CanonicalFamilyIssue(title: str, body: str)` and `canonicalize_family_issue(draft) -> CanonicalFamilyIssue`.

- [ ] **Step 1: Write failing validation and rendering tests**

Assert exact byte boundaries, not character counts, and exact output:

```python
draft = parse_family_issue_draft({
    "title": "Add export",
    "summary": "Users need a portable copy.",
    "context": "The current UI has no export action.",
    "acceptance_criteria": ["A JSON file downloads", "Errors are visible"],
})
self.assertEqual(render_family_issue_body(draft),
    "## Summary\n\nUsers need a portable copy.\n\n"
    "## Context\n\nThe current UI has no export action.\n\n"
    "## Acceptance criteria\n\n- A JSON file downloads\n- Errors are visible\n")
```

Reject empty required values, title newline, invalid UTF-8 surrogates, CR/CRLF, NUL, DEL, every C0/C1 control, U+202A–U+202E and U+2066–U+2069, bool/non-string fields, zero/21 criteria, unknown/missing keys, and all one-byte-over limits. Verify Markdown metacharacters remain literal content under fixed headings and cannot add request-controlled structural fields.

- [ ] **Step 2: Run the test and verify failure**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_family_issue -v`

Expected: FAIL because `family_issue` does not exist.

- [ ] **Step 3: Implement immutable parsing and rendering**

Use named constants and one validator:

```python
TITLE_BYTES = 256
SUMMARY_BYTES = 2 * 1024
CONTEXT_BYTES = 4 * 1024
CRITERION_BYTES = 512
MAX_CRITERIA = 20

def canonicalize_family_issue(draft: FamilyIssueDraft) -> CanonicalFamilyIssue:
    return CanonicalFamilyIssue(draft.title, render_family_issue_body(draft))
```

Normalize nothing: validate and preserve exact accepted Unicode so preview, stored content, and POST bytes are identical.

- [ ] **Step 4: Run the test and lint**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_family_issue -v && bin/lint`

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/agent_container/family_issue.py tests/container/test_family_issue.py
git commit -m "feat: validate canonical family issue drafts"
```

---

### Task 3: Intake protocol and limited container client

**Files:**
- Create: `src/agent_container/family_intake_protocol.py`
- Create: `src/agent_container/family_intake_client.py`
- Create: `container/bin/agent-family`
- Create: `tests/container/test_family_intake_protocol.py`
- Create: `tests/container/test_family_intake_client.py`
- Modify: `tests/container/test_image.py`

**Interfaces:**
- Produces: `FamilyIntakeRequest(version, operation, capability, payload)` and `FamilyIntakeResponse(version, status, request_id, expires_at)`.
- Produces: 4-byte big-endian length-prefixed `encode_*`, `decode_*`, `read_*`, and `write_*`; request maximum is exactly 16,384 bytes.
- Produces CLI: `agent-family issue create --title TITLE --summary SUMMARY --context CONTEXT --acceptance-criterion TEXT [...]`.
- Consumes environment: `AGENT_FAMILY_SOCKET`, `AGENT_FAMILY_CAPABILITY`; neither is printed.

- [ ] **Step 1: Write failing frame and CLI tests**

Test split reads/writes, EOF, zero/oversized/trailing frames, duplicate JSON keys, NaN, wrong version/operation/status, unknown fields, response maximum, socket errors, fixed stderr, and absence of capability, payload, repository, or path in output. Assert success output contains only `pending`, request ID, and expiry.

```python
response = run_create(args, environment=env, connector=fake_connector)
self.assertEqual(response.status, "pending")
self.assertEqual(captured.operation, "issue_create_request")
self.assertNotIn(env["AGENT_FAMILY_CAPABILITY"], stdout.getvalue())
```

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_family_intake_protocol \
  tests.container.test_family_intake_client \
  tests.container.test_image -v
```

Expected: FAIL because protocol, client, and managed executable are absent.

- [ ] **Step 3: Implement the bounded protocol and client**

Validate the draft before connecting. Open only an `AF_UNIX/SOCK_STREAM` socket at the exact absolute managed path, send one request, read one response, reject trailing bytes, and clear local references to capability after use. The wrapper must only import and call `family_intake_client.main()`.

- [ ] **Step 4: Install the managed executable in the image**

Update `Containerfile` to copy `container/bin/agent-family` to `/usr/local/bin/agent-family` with the same root-owned executable mode checks used for other managed clients. Extend image tests to prove approval commands and family credential files are absent.

- [ ] **Step 5: Run focused tests and lint**

Run the Step 2 command, then `bin/lint`.

Expected: PASS.

- [ ] **Step 6: Commit Task 3**

```bash
git add Containerfile container/bin/agent-family \
  src/agent_container/family_intake_protocol.py \
  src/agent_container/family_intake_client.py \
  tests/container/test_family_intake_protocol.py \
  tests/container/test_family_intake_client.py tests/container/test_image.py
git commit -m "feat: add bounded family issue intake client"
```

---

### Task 4: Pending store, lifecycle, cleanup, and content-free audit

**Files:**
- Create: `src/agent_container/family_pending.py`
- Create: `tests/container/test_family_pending.py`

**Interfaces:**
- Produces: `PendingState` enum with `pending`, `sending`, `created`, `rejected`, `expired`, and `unknown`.
- Produces: `PendingRequest(request_id, project_id, created_at, expires_at, state, issue)`.
- Produces: `create_pending(...)`, `load_pending(...)`, `list_pending(...)`, `transition_pending(...)`, `expire_pending(...)`, and `recover_sending(...)`.
- Produces: `append_family_audit(path, *, timestamp, project_id, request_id, operation, status, stage) -> None` with closed vocabularies.

- [ ] **Step 1: Write failing persistence and lifecycle tests**

Use a fixed clock and deterministic random source. Cover 128-bit random lowercase-hex IDs, collision retry, 24-hour expiry, maximum 10 unfinished records, immutable canonical content, exact schema, all allowed and forbidden transitions, concurrent approve/reject/expiry, double approve, startup `sending -> unknown`, and terminal deletion of title/body after created/rejected/expired.

```python
request = create_pending(store, "demo", issue, now=1_800_000_000,
                         random_bytes=lambda size: b"\x11" * size)
self.assertEqual(request.expires_at, 1_800_086_400)
self.assertEqual(request.state, PendingState.PENDING)
self.assertEqual(recover_sending(store, request.request_id).state,
                 PendingState.UNKNOWN)
```

Inject crashes at temp write, file `fsync`, rename, parent `fsync`, lock acquisition, state update, content unlink, and audit append. Assert no success event precedes durable state and cleanup, unknown sibling files cause fail-closed behavior, and audit JSON never contains title/body/repository/URL/exception text.

- [ ] **Step 2: Run the test and verify failure**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_family_pending -v`

Expected: FAIL because the pending store does not exist.

- [ ] **Step 3: Implement descriptor-relative store and state machine**

Persist the canonical content and state together while nonterminal. Serialize transitions with a per-request `flock` lock file held across remote send preparation and final durable transition. Terminal records retain only identifiers, timestamps, state, and created issue number/URL where applicable. Treat any surviving `sending` as unknown during inventory initialization.

- [ ] **Step 4: Implement fixed-schema audit**

Use one `AuditEvent` validator and append a single ASCII JSON line under a locked `0600` file. Reject caller-provided free text; `stage` must be one of `intake`, `validation`, `binding`, `token`, `inventory`, `send`, `response`, `cleanup`, or `reconcile`.

- [ ] **Step 5: Run focused tests and lint**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_family_pending -v && bin/lint`

Expected: PASS.

- [ ] **Step 6: Commit Task 4**

```bash
git add src/agent_container/family_pending.py tests/container/test_family_pending.py
git commit -m "feat: persist family issue approval lifecycle"
```

---

### Task 5: Credential-free intake broker and real Unix socket

**Files:**
- Create: `src/agent_container/family_intake_broker.py`
- Create: `src/agent_container/family_intake_transport.py`
- Create: `src/agent_container/family_intake_runtime.py`
- Create: `tests/container/test_family_intake_broker.py`
- Create: `tests/container/test_family_intake_transport.py`
- Create: `tests/container/test_family_intake_runtime.py`
- Create: `tests/integration/test_family_intake_socket.py`

**Interfaces:**
- Produces: `FamilyIntakeSession(project_id, capability, expires_at, peer_pid, consumed=False)` and `.handle(request) -> FamilyIntakeResponse`.
- Produces: `handle_family_intake_connection(connection, session, store) -> None`.
- Produces: `FamilyIntakeRuntime.start(...) -> FamilyRuntimeMount(socket_dir, capability, environment)` and context-manager cleanup.
- Consumes Tasks 1–4; imports nothing from `github_app`, `github_client`, `github_issue`, or development broker policy/runtime modules.

- [ ] **Step 1: Write failing authorization and broker tests**

Cover wrong project/version/operation/capability, expired or reused capability, peer PID mismatch, malformed schema, count limit, one-request-per-run, disconnect before/after persistence, fixed responses, and the absence of repository/content in responses and audit. Inspect imports to prohibit GitHub credential/network modules.

- [ ] **Step 2: Run unit tests and verify failure**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_family_intake_broker \
  tests.container.test_family_intake_transport \
  tests.container.test_family_intake_runtime -v
```

Expected: FAIL because intake services do not exist.

- [ ] **Step 3: Implement session, peer validation, and single-use persistence**

Read `SO_PEERCRED`, require the expected runtime PID/UID, validate the complete frame before consuming capability, and atomically couple capability consumption with pending creation under the session lock. Return only:

```python
FamilyIntakeResponse(
    version=1, status="pending",
    request_id=pending.request_id,
    expires_at=pending.expires_at,
)
```

- [ ] **Step 4: Implement runtime ownership and fail-closed cleanup**

Create a fresh bounded run directory and socket, pass capability only through the returned container environment, stop accepting after consumption/runtime exit, and remove only inode-validated artifacts created by this instance. Broker death never starts a networked fallback.

- [ ] **Step 5: Write and run real-socket integration tests**

Exercise the actual wrapper and Unix socket for success, stale capability, peer mismatch via a second process, broker death, disconnect, concurrent clients, and cleanup:

Run: `PYTHONPATH=src python3 -m unittest tests.integration.test_family_intake_socket -v`

Expected: PASS on a host supporting Unix peer credentials; otherwise record the explicit platform skip reason.

- [ ] **Step 6: Run all Task 5 tests and lint**

Run the Step 2 and Step 5 commands, then `bin/lint`.

Expected: PASS (or only the documented unsupported-platform integration skip).

- [ ] **Step 7: Commit Task 5**

```bash
git add src/agent_container/family_intake_broker.py \
  src/agent_container/family_intake_transport.py \
  src/agent_container/family_intake_runtime.py \
  tests/container/test_family_intake_broker.py \
  tests/container/test_family_intake_transport.py \
  tests/container/test_family_intake_runtime.py \
  tests/integration/test_family_intake_socket.py
git commit -m "feat: add credential-free family intake broker"
```

---

### Task 6: Dedicated Family GitHub App and exact repository verification

**Files:**
- Create: `src/agent_container/family_github_app.py`
- Create: `tests/container/test_family_github_app.py`

**Interfaces:**
- Produces: `FamilyAppMetadata(client_id, installation_id, private_key)` loaded only from family paths.
- Produces: `FamilyInstallationTokenProvider.get() -> InstallationToken` requiring exact permissions `{"issues": "write", "metadata": "read"}`.
- Produces: `verify_family_repository(token, binding, transport) -> None` using bounded installation repository inventory.
- Reuses only credential-free value types and signing/HTTP primitives from `github_app.py`; never the development App metadata, token provider, repository ID, or policy.

- [ ] **Step 1: Write failing metadata, permission, and inventory tests**

Cover exact private-file checks, duplicate/unknown metadata keys, JWT claims, fixed token endpoint, redirect rejection, response/content-type/size bounds, token non-disclosure, missing/extra permissions, pagination rejection, empty/multiple/name-only/ID-only matches, rename, transfer, and repository selection changes.

```python
self.assertEqual(provider.get().expires_at, 1_800_003_600)
verify_family_repository(token, FamilyBinding(Repository("family", "roadmap"), 42), transport)
```

- [ ] **Step 2: Run the test and verify failure**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_family_github_app -v`

Expected: FAIL because the dedicated family App module is absent.

- [ ] **Step 3: Implement least-privilege token issuance and inventory verification**

Request only `issues:write` and `metadata:read`, reject any returned permission difference, and query only the fixed installation repositories endpoint with bounded `per_page=100`. Require exactly one inventory object whose `full_name` and integer `id` both equal the binding; reject redirects and further pages rather than silently omitting repositories.

- [ ] **Step 4: Run focused tests and lint**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_family_github_app -v && bin/lint`

Expected: PASS.

- [ ] **Step 5: Commit Task 6**

```bash
git add src/agent_container/family_github_app.py \
  tests/container/test_family_github_app.py
git commit -m "feat: add dedicated family GitHub App boundary"
```

---

### Task 7: Create-only GitHub transport and ambiguous outcome contract

**Files:**
- Create: `src/agent_container/family_issue_create.py`
- Create: `tests/container/test_family_issue_create.py`

**Interfaces:**
- Produces: `CreatedIssue(number: int, url: str)`.
- Produces: `SendNotStarted(stage: str)` and `SendOutcomeUnknown(stage: str)` fixed-stage exceptions.
- Produces: `FamilyIssueCreator.create(binding, canonical, tokens) -> CreatedIssue` and `.verify_existing(binding, canonical, issue_number, tokens) -> CreatedIssue`.

- [ ] **Step 1: Write failing endpoint, request, and response tests**

Assert one `POST https://api.github.com/repos/{owner}/{repo}/issues`, headers with the fixed API version, and body exactly `{"title": ..., "body": ...}`. Reject redirects, 401 retry, alternate host/path, labels, excessive response, wrong content type/status/state/number/URL/repository, duplicate keys, and trailing or malformed JSON.

Classify DNS/connect/TLS failure before any request-body byte as `SendNotStarted`; classify timeout, reset, partial response, malformed response, or any failure after body transmission starts as `SendOutcomeUnknown`. Verify there is exactly one transport attempt.

- [ ] **Step 2: Run the test and verify failure**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_family_issue_create -v`

Expected: FAIL because create-only transport is absent.

- [ ] **Step 3: Implement an instrumented one-shot transport**

Use `http.client.HTTPSConnection` without retry or redirect handling and track whether `send()` began. Convert only the pre-send failures to `SendNotStarted`; conservatively convert every later uncertainty to `SendOutcomeUnknown`. Parse a bounded 201 response and require `state == "open"` and exact `https://github.com/{slug}/issues/{number}`.

- [ ] **Step 4: Implement trusted reconciliation lookup**

`verify_existing` performs one bounded GET for the supplied positive issue number and requires exact canonical title/body, repository URL, and issue number before returning. It never searches by title and never mutates the Issue.

- [ ] **Step 5: Run focused tests and lint**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_family_issue_create -v && bin/lint`

Expected: PASS.

- [ ] **Step 6: Commit Task 7**

```bash
git add src/agent_container/family_issue_create.py \
  tests/container/test_family_issue_create.py
git commit -m "feat: add one-shot family issue creation"
```

---

### Task 8: Host binding, inventory, preview, approve, reject, and reconciliation CLI

**Files:**
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_agentctl.py`

**Interfaces:**
- Produces the exact `agentctl family ...` command tree from spec section 8.
- Produces: `_approve_family_issue(..., stdin: TextIO, stdout: TextIO, now: int) -> int` with TTY-only confirmation bound to request ID.
- Consumes Tasks 1, 4, 6, and 7.

- [ ] **Step 1: Write failing parser, bind, list, and doctor tests**

Assert every command and required argument, reject `--yes` and batch forms, require a registered project, validate exact binding name/ID from live inventory, and never overwrite a binding silently. `list` prints fixed identity/state fields. `doctor` separately reports local state, binding, pending invariants, App metadata/permissions, and unobserved remote availability without claiming it PASS.

- [ ] **Step 2: Run focused CLI tests and verify failure**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl.AgentCtlFamilyTest -v`

Expected: FAIL because the family command tree is absent.

- [ ] **Step 3: Implement bind, list, doctor, pending, and preview**

`pending` prints only request ID, project, timestamps, and state. `preview` is the sole read command that prints repository and canonical content. Ensure output helpers keep payload out of exceptions and audit.

- [ ] **Step 4: Write failing approval and race tests**

Cover non-TTY refusal, EOF, wrong confirmation, expired/rejected/unknown/sending request, content or binding swap after preview, App permission drift, concurrent reject/expiry, pre-send failure returning to pending, successful cleanup to created, cleanup failure not reporting success, and post-send uncertainty becoming unknown without retry.

```python
stdin = FakeTTY(f"approve {request_id}\n")
result = main(["family", "issue", "approve", "demo", request_id],
              stdin=stdin, stdout=stdout, family_creator=creator)
self.assertEqual(result, 0)
self.assertEqual(load_pending(store, request_id).state, PendingState.CREATED)
```

- [ ] **Step 5: Implement interactive approval and reject**

Preview the immutable stored content, print target and external effect, then require exact `approve <request-id>` from a TTY. Under the request lock, re-read and compare the pending inode/content and binding, verify inventory, transition durably to sending, call the one-shot creator, then durably transition/clean up to created or unknown. A proven pre-send failure returns to pending. Reject uses the same lock and deletes content before reporting success.

- [ ] **Step 6: Write failing reconciliation tests**

Require unknown state. `resolve-created` validates the supplied issue through `verify_existing` before cleanup/created. `resolve-not-created` requires TTY confirmation `not-created <request-id>`, prominently states that a later approve can create external state, and only then returns to pending. Reject automatic lookup conclusions and concurrent transitions.

- [ ] **Step 7: Implement reconciliation and fixed audit events**

Write only closed-vocabulary events after durable transition and required cleanup. Do not include repository, content, URL, raw exception, token, or remote body.

- [ ] **Step 8: Run CLI tests and lint**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_agentctl -v && bin/lint`

Expected: PASS.

- [ ] **Step 9: Commit Task 8**

```bash
git add src/agent_container/agentctl.py tests/container/test_agentctl.py
git commit -m "feat: add host-approved family issue workflow"
```

---

### Task 9: Codex and Claude runtime wiring with credential non-exposure

**Files:**
- Modify: `src/agent_container/podman.py`
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_podman.py`
- Modify: `tests/container/test_agentctl.py`
- Create: `tests/integration/test_family_intake_podman.py`

**Interfaces:**
- Extends: `run_codex_spec(..., family_mount: FamilyRuntimeMount | None)` and `run_claude_spec(..., family_mount: FamilyRuntimeMount | None)`.
- Runtime environment contains only `AGENT_FAMILY_SOCKET` and `AGENT_FAMILY_CAPABILITY` when a valid binding exists.
- `agentctl run` starts/stops intake in the existing `ExitStack` lifecycle for both agents.

- [ ] **Step 1: Write failing Podman-spec tests**

Assert exactly one read/write socket-directory mount and two family environment entries for bound projects; none for unbound projects. Reject non-absolute/unsafe mounts. Assert command, environment, mounts, labels, and container inspection contain no family App/key/token, repository name/ID, pending host path, or approval command.

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.container.test_podman \
  tests.container.test_agentctl.AgentCtlFamilyRuntimeTest -v
```

Expected: FAIL because family runtime mounts are not wired.

- [ ] **Step 3: Implement optional launch wiring**

Load and validate the binding before runtime creation, start `FamilyIntakeRuntime` only for a bound project, pass the mount directly into the selected spec builder, and register cleanup in `ExitStack`. If intake startup or supervision fails, abort the run; never relaunch without family controls.

- [ ] **Step 4: Add rootless Podman integration coverage**

Run an instrumented image for Codex and Claude paths. Submit one request through the mounted socket, prove the second is denied, inspect `/proc`, environment, mounts, and filesystem for forbidden values, stop the broker, verify no fallback, and validate stale runtime rejection and cleanup.

Run: `PYTHONPATH=src python3 -m unittest tests.integration.test_family_intake_podman -v`

Expected: PASS where rootless Podman is available; otherwise record `not run` with the concrete missing prerequisite.

- [ ] **Step 5: Run focused tests and lint**

Run the Step 2 and Step 4 commands, then `bin/lint`.

Expected: PASS apart from an evidence-backed Podman prerequisite skip.

- [ ] **Step 6: Commit Task 9**

```bash
git add src/agent_container/podman.py src/agent_container/agentctl.py \
  tests/container/test_podman.py tests/container/test_agentctl.py \
  tests/integration/test_family_intake_podman.py
git commit -m "feat: wire family intake into agent runtimes"
```

---

### Task 10: Operations, regression, security review, and real-host smoke gate

**Files:**
- Create: `docs/family-issue-create-broker.md`
- Create: `docs/family-issue-create-broker-smoke-test.md`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `tests/container/test_docs.py`

**Interfaces:**
- Documents provisioning a distinct Family GitHub App, binding, operator workflow, expiry/unknown reconciliation, doctor interpretation, disable/rollback, and evidence labels.
- Produces no automatic real GitHub mutation; the smoke procedure pauses for per-Issue user approval.

- [ ] **Step 1: Write failing documentation contract tests**

Require exact commands, permission set, distinct-App warning, 24-hour/10-request limits, TTY approval syntax, unknown reconciliation, rollback ordering, no-auto-close statement, and `PASS`/`PARTIAL`/`FAIL`/`not run` evidence vocabulary. Reject stale claims that the development App supports Issue write.

- [ ] **Step 2: Run docs tests and verify failure**

Run: `PYTHONPATH=src python3 -m unittest tests.container.test_docs -v`

Expected: FAIL because operations and smoke documents are absent.

- [ ] **Step 3: Write operator and smoke documentation**

The smoke document must require, immediately before creation, displaying the exact test repository, canonical title/body, purpose, and external effect and obtaining fresh user approval. Include commands to prove duplicate rejection, content-free audit, credential non-exposure, terminal cleanup, forced unknown reconciliation, and rollback without modifying created Issues.

- [ ] **Step 4: Run complete automated verification**

Run:

```bash
bin/lint
PYTHONPATH=src python3 -m unittest discover -s tests/codex -v
PYTHONPATH=src python3 -m unittest discover -s tests/container -v
PYTHONPATH=src python3 -m unittest \
  tests.integration.test_github_broker_socket \
  tests.integration.test_handover_broker_socket \
  tests.integration.test_egress_broker_socket \
  tests.integration.test_family_intake_socket -v
git diff --check
```

Expected: all runnable checks PASS; document every skip by missing prerequisite and never relabel it PASS.

- [ ] **Step 5: Run Podman regression where available**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.integration.test_project_image_podman \
  tests.integration.test_egress_podman \
  tests.integration.test_family_intake_podman -v
```

Expected: PASS on the supported rootless Podman host; otherwise `not run` with exact reason.

- [ ] **Step 6: Perform the independent security review gate**

Use `superpowers:requesting-code-review`. Review credential, filesystem, network, authorization, external-state, ambiguity, cleanup, rollback, and cross-broker isolation. Resolve every Critical or Important finding and rerun affected checks before continuing.

- [ ] **Step 7: Pause for real-host authorization**

Do not provision an App/repository or create an Issue under this plan step. Present the exact target repository, canonical title/body, purpose, and effect to the user. Only after explicit approval, execute `docs/family-issue-create-broker-smoke-test.md`; otherwise record real-host smoke as `not run`.

- [ ] **Step 8: Commit Task 10**

```bash
git add README.md CHANGELOG.md docs/family-issue-create-broker.md \
  docs/family-issue-create-broker-smoke-test.md tests/container/test_docs.py
git commit -m "docs: add family issue broker operations"
```

- [ ] **Step 9: Verify the committed branch**

Run `git status --short --branch`, `git log --oneline --decorate -12`, and the complete Step 4 verification again.

Expected: only intended commits, clean worktree, and all runnable checks PASS. Preserve real-host and Podman results exactly as observed.
