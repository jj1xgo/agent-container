# Phase 1 Claude Handover Writer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow Claude Code to create a new project-scoped handover through a fail-closed host writer without granting direct handover-directory write access or weakening either sandbox.

**Architecture:** A dedicated per-runtime Unix-socket broker authenticates the keep-id peer and a random capability, validates a fixed create-only request, and atomically publishes a canonical Markdown handover in the host project directory. The Claude container receives a read-only handover mount plus read-only socket/capability mounts; Codex keeps its existing direct path, and the GitHub broker remains independent.

**Tech Stack:** Python 3.11+, Unix domain sockets, Linux `SO_PEERCRED`, rootless Podman command specs, POSIX file primitives, shell wrapper, `unittest`.

**Spec:** `docs/superpowers/specs/2026-08-27-phase1-claude-handover-writer-design.md`

## Global Constraints

- The broker exposes only `create`; it never lists, reads, edits, renames, overwrites, or deletes a handover.
- Request and rendered-document limits are exactly 65,536 bytes; text is strict UTF-8 and contains no NUL.
- The required seven `##` headings occur exactly once and in the spec order; `###` and lower headings are allowed inside sections.
- Host code generates H1, project, UTC created time, session metadata, filename, and final path. Claude session is `（未記録）` until a trusted host source exists.
- Published handovers are mode `0600`, never replace an existing path, and never expose a partial final document.
- Claude handover directory mount becomes read-only. Outer Podman hardening, Claude nested sandbox, managed policy, token isolation, and GitHub mode remain unchanged.
- Codex behavior and direct writer remain unchanged in Phase 1.
- Broker failure never falls back to direct write or a read-write mount.
- Logs, audit, responses, and errors never contain request body, title, capability, matching credential text, environment values, or raw exception text.
- No new third-party Python dependency is introduced.
- Authenticated Claude/host smoke tests run only after merge and separate user approval; unrun checks stay recorded as `not run`.

---

### Task 1: Fixed Handover Broker Protocol

**Files:**
- Create: `src/agent_container/handover_broker_protocol.py`
- Create: `tests/container/test_handover_broker_protocol.py`

**Interfaces:**
- Produces: `PROTOCOL_VERSION = 1`, `MAX_REQUEST_BYTES = 65_536`, `MAX_DOCUMENT_BYTES = 65_536`.
- Produces: `HandoverRequest(version: int, capability: str, project_id: str, operation: str, title: str, body: str)`.
- Produces: `HandoverResponse(version: int, status: str, path: str, code: str)`.
- Produces: `encode_request_frame`, `read_request_frame`, `encode_response_frame`, `read_response_frame` using a four-byte big-endian length prefix.

- [ ] **Step 1: Write strict request and response tests**

```python
class HandoverBrokerProtocolTest(unittest.TestCase):
    def test_request_round_trip_preserves_only_fixed_schema(self) -> None:
        request = HandoverRequest(
            1, "A" * 43, "agent-container", "create", "Safe title", VALID_BODY
        )
        self.assertEqual(read_request_frame(BytesIO(encode_request_frame(request))), request)

    def test_rejects_duplicate_unknown_and_wrong_typed_fields(self) -> None:
        for payload in (
            b'{"version":1,"version":1}',
            b'{"version":true,"capability":"x","project_id":"p",'
            b'"operation":"create","title":"t","body":"b","extra":1}',
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                decode_request_frame(frame(payload))

    def test_rejects_zero_oversize_truncated_utf8_and_nul_frames(self) -> None:
        invalid = (
            b"\x00\x00\x00\x00",
            struct.pack(">I", MAX_REQUEST_BYTES + 1),
            b"\x00\x00\x00\x05{}",
            b"\x00\x00\x00\x01\xff",
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                read_request_frame(BytesIO(payload))
```

Also test response status/code combinations: `ok` requires a non-empty absolute container path and empty code; `denied` or `error` requires an empty path and one of `authentication`, `schema`, `size`, `content-policy`, `filesystem-boundary`, `write`, `unavailable`.

- [ ] **Step 2: Run protocol tests and verify RED**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker_protocol -v`

Expected: FAIL because `agent_container.handover_broker_protocol` does not exist.

- [ ] **Step 3: Implement strict framed JSON parsing**

```python
PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 65_536
MAX_DOCUMENT_BYTES = 65_536
_REQUEST_FIELDS = frozenset(
    {"version", "capability", "project_id", "operation", "title", "body"}
)
_RESPONSE_FIELDS = frozenset({"version", "status", "path", "code"})

@dataclass(frozen=True)
class HandoverRequest:
    version: int
    capability: str
    project_id: str
    operation: str
    title: str
    body: str
```

Use `json.loads(text, object_pairs_hook=_object_without_duplicates, parse_constant=_reject_constant)`. Decode UTF-8 strictly, reject NUL in every string, reject booleans where integers are required, require the exact field set, and read exactly the declared length without an unbounded `read()`.

- [ ] **Step 4: Run protocol tests and verify GREEN**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker_protocol -v`

Expected: all protocol tests PASS.

- [ ] **Step 5: Commit the protocol**

```bash
git add src/agent_container/handover_broker_protocol.py tests/container/test_handover_broker_protocol.py
git commit -m "feat: add handover broker protocol"
```

### Task 2: Canonical Validation and Atomic Writer

**Files:**
- Create: `src/agent_container/handover_writer.py`
- Create: `tests/container/test_handover_writer.py`
- Read: `src/agent_container/handover.py`
- Read: `src/agent_container/state.py`

**Interfaces:**
- Consumes: `MAX_DOCUMENT_BYTES` from Task 1 and `validate_project_id` from `state.py`.
- Produces: `validate_handover_content(title: str, body: str) -> tuple[str, str]`.
- Produces: `render_handover(project_id: str, title: str, body: str, now: datetime) -> bytes`.
- Produces: `create_atomic_handover(project_dir: Path, project_id: str, title: str, body: str, now: datetime | None = None, token_hex: Callable[[int], str] = secrets.token_hex) -> Path`.

- [ ] **Step 1: Write validator RED tests**

```python
REQUIRED_HEADINGS = (
    "## 作業の目的", "## 現在地", "## 決定事項と理由",
    "## 変更したファイル・commit・PR", "## 検証結果",
    "## 未解決事項とリスク", "## 次の一手",
)

def valid_body() -> str:
    return "\n\n".join(f"{heading}\ncontent" for heading in REQUIRED_HEADINGS) + "\n"

def test_accepts_exact_sections_and_lower_level_headings(self) -> None:
    title, body = validate_handover_content(
        " Safe title ", valid_body().replace("content", "### Detail\ncontent", 1)
    )
    self.assertEqual(title, "Safe title")
    self.assertTrue(body.endswith("\n"))
```

Add table-driven failures for empty/newline title, missing/duplicate/reordered/unknown `##` headings, H1/metadata prefix, NUL, oversized UTF-8 bytes, and credential-like patterns. Use realistic minimum-length literals for `ghp_`, `github_pat_`, boundary-prefixed `sk-`, and PEM headers; include `risk-based` as an accepted false-positive guard.

- [ ] **Step 2: Run validator tests and verify RED**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_handover_writer.HandoverValidationTest -v`

Expected: FAIL because `handover_writer` does not exist.

- [ ] **Step 3: Implement content validation and canonical rendering**

```python
_REQUIRED_HEADINGS = (
    "## 作業の目的",
    "## 現在地",
    "## 決定事項と理由",
    "## 変更したファイル・commit・PR",
    "## 検証結果",
    "## 未解決事項とリスク",
    "## 次の一手",
)
_CREDENTIALS = re.compile(
    r"(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
```

Match only lines starting with `## ` when checking the exact ordered tuple. Normalize only title surrounding whitespace and a single final body newline; do not rewrite section content. Render canonical metadata with `Session: （未記録）` and an aware UTC timestamp.

- [ ] **Step 4: Write atomic writer RED tests**

Test exact filename pattern, UTC conversion, mode `0600`, content bytes, collision retry using deterministic `token_hex`, pre-existing file preservation, symlinked project rejection, project-ID mismatch, and cleanup after injected write/link/fsync failures. Assert no final filename exists after every failure.

- [ ] **Step 5: Implement no-replace atomic publish**

```python
temporary = project_dir / f".handover-{secrets.token_hex(8)}.tmp"
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
# write all bytes, fsync(fd), close, os.link(temporary, final), unlink temp,
# fsync a read-only descriptor for project_dir; cleanup temp in finally.
```

Resolve the already-existing project directory strictly, reject symlinks and a basename different from `project_id`, and retry only a final-name `FileExistsError`, at most eight times. Never use `os.replace`.

- [ ] **Step 6: Run writer tests and verify GREEN**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_handover_writer -v`

Expected: validator and writer tests PASS with no temporary files left behind.

- [ ] **Step 7: Commit validator and writer**

```bash
git add src/agent_container/handover_writer.py tests/container/test_handover_writer.py
git commit -m "feat: add atomic handover writer"
```

### Task 3: Runtime Session, Authorization, and Secret-Free Audit

**Files:**
- Create: `src/agent_container/handover_broker.py`
- Create: `tests/container/test_handover_broker.py`
- Modify: `src/agent_container/state.py`
- Test: `tests/container/test_state.py`

**Interfaces:**
- Produces StateLayout properties `handover_broker_root`, `handover_broker_run_root`, and `handover_broker_audit_file` under the private agent-container state root.
- Produces: `HandoverBrokerSession.create(state_root: Path, project_id: str, project_dir: Path) -> HandoverBrokerSession`.
- Produces: `authorize(request: HandoverRequest, peer_uid: int) -> tuple[str, str]` returning validated title/body.
- Produces: `audit(status: str, *, stage: str, path: str = "") -> None`, `open_listener()`, and `close()`.

- [ ] **Step 1: Write session RED tests**

Create private temporary state and handover roots. Assert creation yields `0700` run directory, `0600` capability/socket, a 43-character URL-safe capability, and a persistent `0600` JSONL audit file outside the ephemeral run directory. Assert authorization rejects wrong UID, capability, version, project, operation, and closed session.

```python
with self.assertRaises(ValueError):
    session.authorize(replace(request, project_id="other"), os.getuid())
self.assertNotIn("A" * 43, audit_file.read_text(encoding="utf-8"))
```

Test cleanup refuses a replaced non-socket/non-regular runtime path, clears the in-memory capability, removes the run directory, and makes old authorization fail.

- [ ] **Step 2: Run session tests and verify RED**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker tests.container.test_state -v`

Expected: FAIL because the session and StateLayout properties are absent.

- [ ] **Step 3: Implement private lifecycle and authorization**

Follow the existing `BrokerSession` ownership/mode/symlink checks without importing or extending GitHub policy classes. Use a separate `handover-broker/r/<project-hash>/<run-id>` tree and `handover-broker/audit/events.jsonl`. Validate peer UID before `secrets.compare_digest`; accept only version 1, exact project, and operation `create`.

- [ ] **Step 4: Implement fixed-schema audit**

Audit records contain only `timestamp`, hashed run label, `project`, `operation`, `status`, `stage`, and optional container-visible `path`. Validate status/stage against fixed sets before JSON serialization. Open audit with `O_APPEND|O_CREAT|O_NOFOLLOW`, mode `0600`, and fsync each record.

- [ ] **Step 5: Run session/state tests and verify GREEN**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker tests.container.test_state -v`

Expected: all tests PASS.

- [ ] **Step 6: Commit session lifecycle**

```bash
git add src/agent_container/handover_broker.py src/agent_container/state.py tests/container/test_handover_broker.py tests/container/test_state.py
git commit -m "feat: add handover broker session"
```

### Task 4: Broker Handler, Client, and Claude Wrapper Mode

**Files:**
- Create: `src/agent_container/handover_broker_transport.py`
- Create: `src/agent_container/handover_broker_client.py`
- Create: `tests/container/test_handover_broker_transport.py`
- Create: `tests/container/test_handover_broker_client.py`
- Modify: `container/bin/agent-handover`
- Modify: `tests/container/test_agent_handover_wrapper.py`

**Interfaces:**
- Consumes Tasks 1-3 and `create_atomic_handover`.
- Produces: `handle_handover_connection(session, connection, peer_uid, now=None) -> int`.
- Produces: `HandoverBrokerClient.create(title: str, body: bytes) -> str`.
- Produces CLI: `python3 -m agent_container.handover_broker_client create --title TITLE`, reading stdin bytes.

- [ ] **Step 1: Write handler RED tests**

Use `BytesIO` and a real session fixture. Assert a valid request produces status `ok`, returns `/handovers/PROJECT/FILENAME`, creates one canonical file, and audits `ok`. Table-test auth, schema, size, content-policy, filesystem, and injected writer failures; each must return a fixed response and create no final file.

Capture audit, response, stdout, and stderr and assert sentinel body/title/capability strings are absent. A client disconnect while writing the response records only fixed stage `response`.

- [ ] **Step 2: Run handler tests and verify RED**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker_transport -v`

Expected: FAIL because the transport module does not exist.

- [ ] **Step 3: Implement one-request handler**

Read one bounded request, authorize before content validation, call the writer once, and map internal failures to fixed response codes. Do not include `str(error)` in response or audit. Close the connection after one response so a capability cannot multiplex hidden operations.

- [ ] **Step 4: Write client and wrapper RED tests**

Test socket/capability owner, mode, type, symlink, exact path, stdin size, missing environment, fixed request project, success path, denied/error exit, and unavailable socket. Extend wrapper tests so:

```python
environment.update({
    "AGENT_HANDOVER_BROKER_SOCKET": str(socket_path),
    "AGENT_HANDOVER_BROKER_CAPABILITY": str(capability_path),
})
completed = subprocess.run(
    (str(WRAPPER), "create", "--title", "Safe title"),
    env=environment,
    input=VALID_BODY,
    capture_output=True,
    text=True,
    check=False,
)
```

Assert Claude broker mode never invokes direct `handover_cli`, rejects scope override arguments, and does not echo rejected stdin. Keep the existing Codex direct-mode test unchanged and green.

- [ ] **Step 5: Implement client and dual-mode wrapper**

```sh
if [ -n "${AGENT_HANDOVER_BROKER_SOCKET:-}" ] || \
   [ -n "${AGENT_HANDOVER_BROKER_CAPABILITY:-}" ]; then
    : "${AGENT_HANDOVER_BROKER_SOCKET:?required}"
    : "${AGENT_HANDOVER_BROKER_CAPABILITY:?required}"
    exec python3 -m agent_container.handover_broker_client create --title="$title"
fi
```

The Python client reads at most `MAX_REQUEST_BYTES + 1`, validates socket/capability files like the GitHub client, sets a finite timeout, sends exactly one request, and prints only the returned path. It never calls `create_handover` locally.

- [ ] **Step 6: Run transport/client/wrapper tests and verify GREEN**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker_transport tests.container.test_handover_broker_client tests.container.test_agent_handover_wrapper -v`

Expected: all tests PASS, including unchanged Codex behavior.

- [ ] **Step 7: Commit transport and client**

```bash
git add src/agent_container/handover_broker_transport.py src/agent_container/handover_broker_client.py container/bin/agent-handover tests/container/test_handover_broker_transport.py tests/container/test_handover_broker_client.py tests/container/test_agent_handover_wrapper.py
git commit -m "feat: route Claude handovers through broker"
```

### Task 5: Host Runtime and Real Unix-Socket Integration

**Files:**
- Create: `src/agent_container/handover_broker_runtime.py`
- Create: `tests/container/test_handover_broker_runtime.py`
- Create: `tests/integration/test_handover_broker_socket.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes `HandoverBrokerSession` and `handle_handover_connection`.
- Produces: `HandoverRuntimeMount(run_dir: Path)` with socket/capability paths derived from `run_dir`.
- Produces: `HandoverBrokerRuntime.create(layout: StateLayout, project_dir: Path)` and context-manager return type `HandoverRuntimeMount`.

- [ ] **Step 1: Write runtime RED tests**

Assert the listener thread starts before `__enter__` returns, obtains peer credentials with `SO_PEERCRED`, survives one denied/known connection, and accepts a later valid connection. Test start failure cleanup, handler exception capture, stop timeout, idempotent session cleanup, and no stale capability reuse.

- [ ] **Step 2: Run runtime tests and verify RED**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker_runtime -v`

Expected: FAIL because runtime module does not exist.

- [ ] **Step 3: Implement bounded accept loop**

Use a daemon thread, listener timeout `0.2`, backlog `4`, per-client timeout, and Linux peer credentials:

```python
credentials = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
_pid, peer_uid, _gid = struct.unpack("3i", credentials)
```

Pass only `peer_uid` to the handler. On shutdown, set the stop event, close listener, join for two seconds, close session, and raise a secret-free `HandoverBrokerRuntimeError` if the thread failed or did not stop.

- [ ] **Step 4: Write real socket integration RED test**

Create a real runtime and invoke `HandoverBrokerClient` across its Unix socket. Assert canonical output, exact project confinement, invalid capability denial, a second valid request after denial, audit secret absence, and removal of run directory after context exit.

- [ ] **Step 5: Enable integration test in CI and verify GREEN**

Add `tests.integration.test_handover_broker_socket` to the existing socket integration step with `AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1`.

Run:

```bash
AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest tests.integration.test_handover_broker_socket -v
```

Expected: real socket integration PASS and runtime directory absent afterward.

- [ ] **Step 6: Commit runtime and integration**

```bash
git add src/agent_container/handover_broker_runtime.py tests/container/test_handover_broker_runtime.py tests/integration/test_handover_broker_socket.py .github/workflows/ci.yml
git commit -m "feat: add handover broker runtime"
```

### Task 6: Claude Podman Mounts and Agentctl Orchestration

**Files:**
- Modify: `src/agent_container/podman.py`
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_podman.py`
- Modify: `tests/container/test_agentctl.py`

**Interfaces:**
- Consumes `HandoverRuntimeMount` and `HandoverBrokerRuntime` from Task 5.
- Changes: `run_claude_spec(layout: StateLayout, handover_project: Path, image: str, uid: int, gid: int, handover_broker: HandoverRuntimeMount, broker: BrokerRuntimeMount | None = None) -> CommandSpec`; handover broker is required for Claude.
- Leaves: `run_codex_spec` signature and behavior unchanged.

- [ ] **Step 1: Write Claude command-spec RED tests**

Assert the Claude spec contains:

```text
src=HOST_HANDOVER_PROJECT,dst=/handovers/PROJECT,ro=true
src=RUN_DIR,dst=/run/agent-handover,ro=true
AGENT_HANDOVER_BROKER_SOCKET=/run/agent-handover/broker.sock
AGENT_HANDOVER_BROKER_CAPABILITY=/run/agent-handover/capability
```

Assert it contains no read-write handover mount, no host root, no other project path, and no capability value. Run the existing Codex mount test unchanged.

- [ ] **Step 2: Run podman tests and verify RED**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_podman -v`

Expected: FAIL because `run_claude_spec` lacks the handover runtime mount and uses a read-write handover mount.

- [ ] **Step 3: Implement `HandoverRuntimeMount` rendering**

Add a frozen dataclass in `podman.py`, parallel to `BrokerRuntimeMount`, with a fixed container target `/run/agent-handover`. Mount the run directory read-only and change only Claude's handover project tuple to `read_only=True`. Preserve every existing hardening argument.

- [ ] **Step 4: Write agentctl lifecycle RED tests**

Patch `HandoverBrokerRuntime.create` and verify it is entered only for Claude, after preflight/policy/state setup and before building the Claude command. Cover all four combinations of agent (`codex`, `claude`) and GitHub broker (off/on). For Claude+GitHub, assert both contexts are active and independently passed to the builder. Make handover broker enter fail and assert the Podman runtime command is never called.

- [ ] **Step 5: Implement orchestration with `ExitStack`**

```python
with ExitStack() as stack:
    github_mount = (
        stack.enter_context(UploadPackBrokerRuntime.create(layout, record))
        if arguments.github_broker else None
    )
    handover_mount = (
        stack.enter_context(HandoverBrokerRuntime.create(layout, handover_project))
        if arguments.agent == "claude" else None
    )
    # Build Codex with the unchanged call shape; build Claude with required handover_mount.
```

Read project metadata once when either broker needs it. Do not start a handover runtime for Codex. Ensure context exit happens for runner nonzero return and exceptions.

- [ ] **Step 6: Run podman/agentctl tests and verify GREEN**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_podman tests.container.test_agentctl -v`

Expected: all tests PASS with Codex call-shape regression coverage intact.

- [ ] **Step 7: Commit runtime wiring**

```bash
git add src/agent_container/podman.py src/agent_container/agentctl.py tests/container/test_podman.py tests/container/test_agentctl.py
git commit -m "feat: isolate Claude handover writes"
```

### Task 7: Managed Claude Instructions, Image Contract, and Doctor

**Files:**
- Create: `profiles/claude/CLAUDE.md`
- Modify: `Containerfile`
- Modify: `.containerignore`
- Modify: `src/agent_container/claude_policy.py`
- Modify: `src/agent_container/podman.py`
- Modify: `src/agent_container/agentctl.py`
- Modify: `tests/container/test_image.py`
- Modify: `tests/container/test_claude_policy.py`
- Modify: `tests/container/test_agentctl.py`

**Interfaces:**
- Produces managed memory at `/etc/claude-code/CLAUDE.md`.
- Produces image probe command `handover_broker_client_status_spec(image: str) -> CommandSpec`, invoking `python3 -m agent_container.handover_broker_client --self-check` without mounts.
- Produces Claude doctor check `claude-handover-client`.

- [ ] **Step 1: Write managed-instruction and image RED tests**

Assert `profiles/claude/CLAUDE.md` instructs Claude to use only `agent-handover create --title`, pipe exactly seven completed sections through stdin, verify facts first, omit credentials/transcripts, and never weaken sandbox/mounts or fall back after denial. Assert Containerfile copies it mode `0644` to `/etc/claude-code/CLAUDE.md`, and `.containerignore` allowlists exactly that file.

The location is the Linux managed-policy memory path documented by Anthropic: `https://code.claude.com/docs/en/memory`.

- [ ] **Step 2: Write policy and doctor RED tests**

Extend policy validation so the managed CLAUDE.md must be root-owned, regular, non-symlink, mode `0644`, and have the exact repository profile bytes. Test tampering, FIFO, symlink, wrong owner/mode, and missing file. Add a mount-free hardened image self-check to Claude doctor and assert its stdout/stderr are suppressed on failure.

- [ ] **Step 3: Run tests and verify RED**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_image tests.container.test_claude_policy tests.container.test_agentctl -v`

Expected: FAIL because managed memory and client self-check are absent.

- [ ] **Step 4: Implement managed instructions and validation**

Copy the profile into the image. Update `EXPECTED_*` policy validation to compare exact bytes without logging content. Add `--self-check` to the broker client; it validates only module constants and exits without reading environment, capability, stdin, socket, or handover files.

- [ ] **Step 5: Implement doctor probe**

Use the existing noninteractive hardened Podman prefix with no mounts. Add `PASS claude-handover-client: available` only when the image-local self-check exits zero; on failure return a fixed FAIL detail without captured content.

- [ ] **Step 6: Run image/policy/doctor tests and verify GREEN**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_image tests.container.test_claude_policy tests.container.test_agentctl -v`

Expected: all tests PASS.

- [ ] **Step 7: Commit managed instructions and doctor**

```bash
git add profiles/claude/CLAUDE.md Containerfile .containerignore src/agent_container/claude_policy.py src/agent_container/podman.py src/agent_container/agentctl.py tests/container/test_image.py tests/container/test_claude_policy.py tests/container/test_agentctl.py
git commit -m "feat: add managed Claude handover workflow"
```

### Task 8: Operator Documentation, Smoke Gate, and Final Verification

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/phase2-claude-code.md`
- Modify: `docs/phase2-smoke-test.md`
- Modify: `docs/codex-operations.md`
- Modify: `tests/container/test_docs.py`

**Interfaces:**
- Documents the final command, mount boundary, failure behavior, audit boundary, Codex difference, and post-merge real-host gate.
- Leaves every real-host observation `not run` until separately authorized and executed.

- [ ] **Step 1: Write documentation contract RED tests**

Add assertions that the Claude guide and smoke checklist state:

```text
agent-handover create --title
handover project mount is read-only
broker supports create only
no direct-write fallback
body/title/capability are not audited
other projects are unavailable
authenticated Claude smoke is not run
```

Assert `docs/codex-operations.md` says Codex retains its direct path in Phase 1.

- [ ] **Step 2: Run documentation tests and verify RED**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_docs -v`

Expected: FAIL because the new operational boundary is undocumented.

- [ ] **Step 3: Update operator docs and changelog**

Add concise operator steps for preparing the seven sections and piping them without shell-history body exposure; recommend a private workspace temporary file with mode `0600`, then stdin redirection, and require removal only by the user/operator's normal secure workflow. Do not show credentials or environment dumps. Add smoke rows as `not run` for successful create, direct mutation denial, cross-project denial, secret rejection, non-logging, and expired runtime capability.

- [ ] **Step 4: Run documentation tests and verify GREEN**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_docs -v`

Expected: all documentation tests PASS.

- [ ] **Step 5: Run the full local verification matrix**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest -q
AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH=src python3 -m unittest \
  tests.integration.test_handover_broker_socket -v
git diff --check
git status --short --branch
```

Expected: all tests PASS, only the four existing host-dependent skips remain in the normal suite, socket integration PASS, diff check clean, and only intended files differ from the base.

- [ ] **Step 6: Perform independent security review**

Use `superpowers:requesting-code-review`. Give the reviewer the spec, this plan, base SHA, and HEAD SHA. Require explicit findings for arbitrary filesystem access, overwrite/delete, symlink race, partial publish, capability/peer auth, logging leaks, fallback, mount regression, cleanup, and Codex regression. Fix all Critical and Important findings with a new RED/GREEN cycle; record justified responses to Minor findings.

- [ ] **Step 7: Commit documentation**

```bash
git add README.md CHANGELOG.md docs/phase2-claude-code.md docs/phase2-smoke-test.md docs/codex-operations.md tests/container/test_docs.py
git commit -m "docs: add Claude handover broker operations"
```

- [ ] **Step 8: Push a non-main branch and create the implementation PR**

```bash
git push -u origin feat/claude-handover-writer
gh pr create --base main --head feat/claude-handover-writer \
  --title "Add Claude handover writer" \
  --body-file /tmp/claude-handover-writer-pr.md
gh pr checks --watch --interval 10
```

The PR body must list RED/GREEN evidence, full-suite count, socket integration result, independent review result, direct-write denial boundary, and `not run` authenticated host smoke. Do not merge or perform the real-host smoke without the user's explicit approval at that gate.
