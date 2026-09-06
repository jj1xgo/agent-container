# Phase 6 stage 2 S2-1: kernel guarantees, handover and egress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the five stage 2 kernel guarantees (K1 fail-closed lifecycle, K2 identity-checked `RuntimeArtifacts`, K3 per-connection peer policy, K4 typed frame errors, K5 audit envelope) to `src/agent_container/broker/` and move the handover (H1〜H4) and egress (E5〜E6) brokers onto them.

**Architecture:** The kernel stays a one-way dependency (`handover_*`／`egress_*` → `broker/`). Each guarantee lands as its own kernel module or method with its own unit tests first; the two brokers then replace their compatibility code with the kernel call. Wire bytes and audit bytes do not change; every intentional behavior change is tagged with the spec item number in the commit message and PR body.

**Tech Stack:** Python 3 standard library only (`os`, `stat`, `socket`, `struct`, `threading`, `json`, `datetime`), `unittest`, `ruff` via `bin/lint`.

**Spec:** `docs/superpowers/specs/2026-09-06-broker-kernel-stage2-design.md` (sections K1〜K5, handover H1〜H4, egress E5〜E6, 「PR分割と受け入れ条件」). Facts: `docs/superpowers/plans/2026-09-06-broker-kernel-stage2-investigation.md`.

## Global Constraints

- Kernel `src/agent_container/broker/` must not import `handover_*`, `egress_*`, `github_*`, `family_*`, or `agent_container.state` (`broker/__init__.py`).
- Wire format and `PROTOCOL_VERSION = 1` are unchanged; frame goldens (`tests/container/test_broker_frame_golden.py`, `test_broker_egress_golden.py`, `tests/fixtures/broker_github_golden.json`, `tests/fixtures/broker_family_golden.json`) must pass **without edits**.
- Audit line bytes are unchanged; audit goldens (`tests/container/test_broker_audit_golden.py`, `test_broker_egress_golden.py`) must pass **without edits**.
- Error messages stay fixed strings; causes are suppressed with `from None`; no private path or exception text reaches CLI output.
- Every existing test that is edited must be listed in the PR body with its spec item number (K1〜K5, H1〜H4, E5〜E6). An edit that cannot be tagged means behavior changed unintentionally: stop and report.
- GitHub (`github_broker*.py`) and Family (`family_*.py`) are **not** touched in this PR. `append_text_record` stays in `broker/audit.py` because `github_broker.py` still uses it (removed in S2-2).
- Merge gate: stage 1's real-host smoke (6-6) runs on current `main` before this PR merges (spec「PR分割と受け入れ条件」). This plan does not perform 6-6.
- Local commands (repo root): `bin/lint`; `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest <module>`; full container suite `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests/container`; socket integration `AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.integration.test_handover_broker_socket tests.integration.test_egress_broker_socket tests.integration.test_github_broker_socket`.
- Git: this sandbox cannot create worktrees (`.git/worktrees` is read-only). Work on branch `feat/broker-kernel-stage2-1` checked out in `/workspace` from `main`; return `/workspace` to `main` when the session ends. Never touch the existing `.worktrees/` checkouts.

---

## File structure

| File | Responsibility |
| --- | --- |
| `src/agent_container/broker/frame.py` (modify) | K4: `FrameError` hierarchy, `read_exact(initial_eof=)` |
| `src/agent_container/broker/audit.py` (modify) | K5: `validate_envelope`, applied in `AuditLog.append` |
| `src/agent_container/broker/peer.py` (create) | K3: `PeerPolicy` protocol, `SameUser` |
| `src/agent_container/broker/runtime.py` (modify) | K3: `Connection.peer_pid`／`peer_gid`, `admit_connection`, `SocketBrokerRuntime.peer_policy`; K1: `_try_deactivate`, `deactivate_error`, new `stop` contract; K2 (Task 8): delete `remove_runtime_artifacts` |
| `src/agent_container/broker/artifacts.py` (create) | K2: `RuntimeArtifacts` |
| `src/agent_container/handover_broker.py` (modify) | H2: session cleanup via `RuntimeArtifacts` |
| `src/agent_container/handover_broker_transport.py` (modify) | H1: `read_request_frame` + `FrameSizeError` mapping |
| `src/agent_container/egress_broker.py` (modify) | E5: session cleanup via `RuntimeArtifacts` |
| `tests/container/test_broker_frame.py` (modify) | K4 tests |
| `tests/container/test_broker_audit.py` (modify) | K5 tests; existing records become envelope-conforming |
| `tests/container/test_broker_peer.py` (create) | K3 tests |
| `tests/container/test_broker_runtime.py` (modify) | K3 `Connection` equality, `peer_policy` tests; `make_runtime(deactivate=)`; K2: delete `RemoveRuntimeArtifactsTest` |
| `tests/container/test_broker_deactivate_failure.py` (create) | K1 tests |
| `tests/container/test_broker_artifacts.py` (create) | K2 tests |
| `tests/container/test_handover_broker.py` (modify) | H2: add replaced-socket-inode case |
| `tests/container/test_handover_broker_runtime.py` (modify) | H3: deactivate failure through the handover runtime |
| `tests/container/test_egress_broker.py` (modify) | E5: add replaced-socket-inode case |
| `tests/container/test_egress_broker_runtime.py` (modify) | E6: deactivate failure through the egress runtime |
| `CHANGELOG.md` (modify) | Unreleased entry for S2-1 and #98 |

---

### Task 1: K4 typed frame errors and `read_exact(initial_eof=)`

**Files:**
- Modify: `src/agent_container/broker/frame.py`
- Test: `tests/container/test_broker_frame.py`

**Interfaces:**
- Produces: `FrameError(ValueError)`, `FrameIncomplete`, `FrameSizeError`, `FrameJsonError`, `FrameSchemaError`, `StreamError` (all in `agent_container.broker.frame`); `read_exact(stream, size, *, label, initial_eof=False)`.
- Messages are byte-identical to today; only the exception class becomes more specific.

- [ ] **Step 1: Write the failing tests**

Append to `tests/container/test_broker_frame.py` (imports at the top of the file: add `FrameError, FrameIncomplete, FrameJsonError, FrameSchemaError, FrameSizeError, StreamError` to the existing `from agent_container.broker.frame import ...` lines):

```python
class FrameErrorKindsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = FrameSchema(
            label="test request",
            stream_label="test stream",
            fields=frozenset({"a"}),
            max_bytes=16,
            json=JsonOptions(),
        )

    def test_decode_failures_use_specific_subclasses_with_unchanged_messages(self) -> None:
        cases = (
            (b"\x00\x00", FrameIncomplete, "test request frame is incomplete"),
            (b"\x00\x00\x00\x00", FrameSizeError, "test request frame size is invalid"),
            (b"\x00\x00\x00\x05abc", FrameIncomplete, "test request frame is incomplete"),
            (b"\x00\x00\x00\x03{a}", FrameJsonError, "test request JSON is invalid"),
            (b"\x00\x00\x00\x07{\"b\":1}", FrameSchemaError, "test request schema is invalid"),
        )
        for data, kind, message in cases:
            with self.subTest(data=data), self.assertRaises(kind) as raised:
                decode_frame(self.schema, data)
            self.assertEqual(str(raised.exception), message)
            self.assertIsInstance(raised.exception, FrameError)
            self.assertIsInstance(raised.exception, ValueError)

    def test_encode_failures_use_schema_and_size_subclasses(self) -> None:
        with self.assertRaises(FrameSchemaError) as raised:
            encode_frame(self.schema, {"a": object()})
        self.assertEqual(str(raised.exception), "test request is invalid")
        with self.assertRaises(FrameSizeError) as raised:
            encode_frame(self.schema, {"a": "x" * 32})
        self.assertEqual(str(raised.exception), "test request is too large")

    def test_stream_failures_use_stream_error(self) -> None:
        class Broken:
            def read(self, size: int) -> bytes:
                raise OSError("private-marker")

        with self.assertRaises(StreamError) as raised:
            read_exact(Broken(), 4, label="test stream")
        self.assertEqual(str(raised.exception), "test stream is invalid")
        with self.assertRaises(StreamError) as raised:
            read_exact(BytesIO(b"ab"), 4, label="test stream")
        self.assertEqual(str(raised.exception), "test stream is incomplete")
        with self.assertRaises(FrameSizeError):
            read_frame(self.schema, BytesIO(b"\x00\x00\x00\x00"))

    def test_read_exact_initial_eof_returns_empty_only_before_any_byte(self) -> None:
        self.assertEqual(read_exact(BytesIO(b""), 4, label="test stream", initial_eof=True), b"")
        with self.assertRaises(StreamError) as raised:
            read_exact(BytesIO(b"ab"), 4, label="test stream", initial_eof=True)
        self.assertEqual(str(raised.exception), "test stream is incomplete")
        with self.assertRaises(StreamError):
            read_exact(BytesIO(b""), 4, label="test stream")

    def test_write_all_failure_uses_stream_error(self) -> None:
        class Stuck:
            def write(self, body: bytes) -> int:
                return 0

            def flush(self) -> None:
                pass

        with self.assertRaises(StreamError) as raised:
            write_all(Stuck(), b"abc", label="test stream")
        self.assertEqual(str(raised.exception), "test stream write failed")
```

If `BytesIO`, `JsonOptions`, `read_frame`, or `write_all` are not already imported in the test module, add them (`from io import BytesIO`; the kernel names from `agent_container.broker.frame`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_broker_frame -v 2>&1 | tail -15`
Expected: `ImportError` for `FrameError` (module has no such name).

- [ ] **Step 3: Implement the error hierarchy**

In `src/agent_container/broker/frame.py`, after `HEADER_BYTES = 4`:

```python
class FrameError(ValueError):
    """Every framing failure. Messages are unchanged from stage 1; only the class is specific."""


class FrameIncomplete(FrameError):
    """Fewer bytes than the header or the announced body."""


class FrameSizeError(FrameError):
    """A zero, oversized, or otherwise unacceptable length."""


class FrameJsonError(FrameError):
    """The body is not the accepted JSON subset."""


class FrameSchemaError(FrameError):
    """The decoded object or the values to encode do not match the schema."""


class StreamError(FrameError):
    """Reading from or writing to the underlying stream failed."""
```

Then replace the raises:

- `encode_frame`: `raise FrameSchemaError(f"{schema.label} is invalid") from None`; `raise FrameSizeError(f"{schema.label} is too large")`.
- `decode_frame`: the two `frame is incomplete` raises → `FrameIncomplete`; `frame size is invalid` → `FrameSizeError`; `JSON is invalid` → `FrameJsonError`; `schema is invalid` → `FrameSchemaError`.
- `read_exact` becomes:

```python
def read_exact(
    stream: BinaryIO, size: int, *, label: str, initial_eof: bool = False
) -> bytes:
    output = bytearray()
    while len(output) < size:
        try:
            chunk = stream.read(size - len(output))
        except (OSError, TypeError, ValueError):
            raise StreamError(f"{label} is invalid") from None
        if not isinstance(chunk, bytes):
            raise StreamError(f"{label} is incomplete")
        if not chunk:
            if initial_eof and not output:
                return b""
            raise StreamError(f"{label} is incomplete")
        if len(chunk) > size - len(output):
            raise StreamError(f"{label} is incomplete")
        output.extend(chunk)
    return bytes(output)
```

- `read_frame`: `frame size is invalid` → `FrameSizeError`; the final `frame is invalid` → `FrameError`.
- `write_all`: `write failed` → `StreamError`.
- `write_chunk_stream`: `chunk is invalid` → `FrameSizeError`. `iter_chunk_stream`: `chunk is invalid` and `is too large` → `FrameSizeError`; `limit is invalid` stays `ValueError`.

- [ ] **Step 4: Run the frame tests and goldens**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_broker_frame tests.container.test_broker_frame_golden tests.container.test_broker_egress_golden tests.container.test_broker_github_golden tests.container.test_broker_family_golden tests.container.test_broker_github_primitives tests.container.test_github_broker_compatibility tests.container.test_family_kernel_compatibility -v 2>&1 | tail -5`
Expected: all PASS (existing `assertRaises(ValueError)` still hold because every class subclasses `ValueError`).

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/broker/frame.py tests/container/test_broker_frame.py
git commit -m "feat: type broker frame errors and add initial EOF reads (K4)"
```

---

### Task 2: K5 audit envelope validation

**Files:**
- Modify: `src/agent_container/broker/audit.py`
- Test: `tests/container/test_broker_audit.py`

**Interfaces:**
- Produces: `validate_envelope(record: Mapping[str, object], *, label: str) -> None` raising `ValueError(f"{label} record is invalid")`; `AuditLog.append` calls it before opening the descriptor.
- Required keys: `timestamp` (ISO 8601 string accepted by `datetime.fromisoformat`), `run`, `project`, `operation` (non-empty `str`), `status` in `{"ok", "denied", "error"}`; optional `stage` (non-empty `str`); any other key passes untouched. Key order is never changed.

- [ ] **Step 1: Make existing kernel audit tests envelope-conforming and add the K5 test**

Edit `tests/container/test_broker_audit.py`. Add at module level after `LABEL = "test audit"`:

```python
RECORD = {
    "timestamp": "2026-09-06T00:00:00+00:00",
    "run": "0123456789abcdef",
    "project": "demo",
    "operation": "create",
    "status": "ok",
}


def record(**extra: object) -> dict[str, object]:
    return {**RECORD, **extra}
```

Replace the appends (tag: K5):

- L26-27 in `test_validate_creates_a_private_empty_file_and_append_writes_ascii_lines`: `log.append(record(path="/x"))` and `log.append(record(status="denied", stage="schema"))`; expected bytes become

```python
b'{"timestamp":"2026-09-06T00:00:00+00:00","run":"0123456789abcdef","project":"demo","operation":"create","status":"ok","path":"/x"}\n'
b'{"timestamp":"2026-09-06T00:00:00+00:00","run":"0123456789abcdef","project":"demo","operation":"create","status":"denied","stage":"schema"}\n'
```

- L38 in `test_append_escapes_non_ascii_and_preserves_key_order`: `AuditLog(path, label=LABEL).append(record(b="é", a=1))`; expected `b'{"timestamp":"2026-09-06T00:00:00+00:00","run":"0123456789abcdef","project":"demo","operation":"create","status":"ok","b":"\\u00e9","a":1}\n'`.
- L142 and L156: `AuditLog(path, label=LABEL).append(record())`.

Add:

```python
class AuditEnvelopeTest(unittest.TestCase):
    def test_append_requires_common_keys_and_leaves_broker_keys_alone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            log = AuditLog(path, label=LABEL)
            log.append(record(agent="codex", bytes=5, stage="policy", status="denied"))
            self.assertIn(b'"agent":"codex","bytes":5', path.read_bytes())

            bad_records = (
                {key: value for key, value in RECORD.items() if key != "timestamp"},
                record(timestamp="not a time"),
                record(timestamp=1800000000),
                record(run=""),
                record(project=None),
                record(operation=3),
                record(status="pending"),
                record(status=True),
                record(stage=""),
                record(stage=7),
            )
            for bad in bad_records:
                with self.subTest(record=bad), self.assertRaises(ValueError) as raised:
                    log.append(bad)
                self.assertEqual(str(raised.exception), "test audit record is invalid")
            self.assertEqual(path.read_bytes().count(b"\n"), 1)
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_broker_audit -v 2>&1 | tail -8`
Expected: `AuditEnvelopeTest` FAILS (no `ValueError` raised for bad records); the edited tests PASS.

- [ ] **Step 3: Implement `validate_envelope`**

In `src/agent_container/broker/audit.py` add `from datetime import datetime` and:

```python
_STATUSES = frozenset({"ok", "denied", "error"})
_TEXT_KEYS = ("run", "project", "operation")


def validate_envelope(record: Mapping[str, object], *, label: str) -> None:
    """Every broker audit line carries the same five keys; broker keys pass through."""
    error = ValueError(f"{label} record is invalid")
    if not isinstance(record, Mapping):
        raise error
    timestamp = record.get("timestamp")
    if not isinstance(timestamp, str):
        raise error
    try:
        datetime.fromisoformat(timestamp)
    except ValueError:
        raise error from None
    for key in _TEXT_KEYS:
        value = record.get(key)
        if not isinstance(value, str) or not value:
            raise error
    status = record.get("status")
    if not isinstance(status, str) or status not in _STATUSES:
        raise error
    if "stage" in record:
        stage = record["stage"]
        if not isinstance(stage, str) or not stage:
            raise error
```

In `AuditLog.append`, as the first statement: `validate_envelope(record, label=self.label)`.

- [ ] **Step 4: Run audit tests, goldens, and the two brokers' session tests**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_broker_audit tests.container.test_broker_audit_golden tests.container.test_broker_egress_golden tests.container.test_handover_broker tests.container.test_egress_broker -v 2>&1 | tail -5`
Expected: all PASS (handover and egress records already carry the five keys).

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/broker/audit.py tests/container/test_broker_audit.py
git commit -m "feat: validate the common audit envelope in AuditLog.append (K5)"
```

---

### Task 3: K3 peer identity and per-connection policy

**Files:**
- Create: `src/agent_container/broker/peer.py`
- Modify: `src/agent_container/broker/runtime.py` (`Connection`, `open_connection`, `SocketBrokerRuntime`)
- Create: `tests/container/test_broker_peer.py`
- Modify: `tests/container/test_broker_runtime.py`

**Interfaces:**
- Produces: `Connection(client, stream, peer_uid, peer_pid, peer_gid)` (frozen dataclass, positional order as listed); `PeerPolicy` protocol with `admit(self, connection: Connection) -> bool`; `SameUser` (admits `peer_uid == os.getuid()`); `admit_connection(client, *, timeout, policy: PeerPolicy | None) -> Connection | None`; `SocketBrokerRuntime(peer_policy: PeerPolicy | None = None)`.
- Denied connections: stream closed, no bytes read or written, handler not called, client closed by the runtime (`with client:` inline, `finally: client.close()` thread). A policy that raises is a runtime failure (`failed`).

- [ ] **Step 1: Write the failing tests**

Create `tests/container/test_broker_peer.py`:

```python
import os
import unittest

from agent_container.broker.peer import PeerPolicy
from agent_container.broker.peer import SameUser
from agent_container.broker.runtime import Connection
from agent_container.broker.runtime import admit_connection
from tests.container.test_broker_runtime import FakeClient


class Deny:
    def admit(self, connection: Connection) -> bool:
        return False


class Explode:
    def admit(self, connection: Connection) -> bool:
        raise RuntimeError("private-policy-marker")


class PeerPolicyTest(unittest.TestCase):
    def test_same_user_admits_only_the_running_user(self) -> None:
        policy: PeerPolicy = SameUser()
        own = Connection(None, None, os.getuid(), 1, 1)
        other = Connection(None, None, os.getuid() + 1, 1, 1)
        self.assertTrue(policy.admit(own))
        self.assertFalse(policy.admit(other))

    def test_admit_connection_keeps_all_peer_credentials(self) -> None:
        client = FakeClient(4040)
        connection = admit_connection(client, timeout=7, policy=None)
        self.assertEqual(connection, Connection(client, client.stream, 4040, 1234, 5678))
        self.assertEqual(client.timeout, 7)
        self.assertFalse(client.stream.closed)

    def test_denied_connection_is_closed_without_reading_or_writing(self) -> None:
        client = FakeClient(os.getuid())
        self.assertIsNone(admit_connection(client, timeout=7, policy=Deny()))
        self.assertTrue(client.stream.closed)
        self.assertEqual(client.stream.outgoing.getvalue(), b"")

    def test_policy_failure_closes_the_stream_and_propagates(self) -> None:
        client = FakeClient(os.getuid())
        with self.assertRaisesRegex(RuntimeError, "private-policy-marker"):
            admit_connection(client, timeout=7, policy=Explode())
        self.assertTrue(client.stream.closed)
```

In `tests/container/test_broker_runtime.py`:

- Change L574 to `self.assertEqual(connection, Connection(client, client.stream, 4040, 1234, 5678))` (tag: K3).
- Add to `SocketBrokerRuntimeTest`:

```python
    def test_peer_policy_denial_skips_handler_and_closes_client(self) -> None:
        denied = FakeClient(os.getuid())
        admitted = FakeClient(os.getuid())
        listener = FakeListener((denied, admitted))
        seen: list[int] = []
        handled = threading.Event()

        class SecondOnly:
            def admit(self, connection: Connection) -> bool:
                return connection.client is admitted

        def handler(connection: Connection) -> int:
            seen.append(connection.peer_pid)
            handled.set()
            return 0

        runtime, calls = make_runtime(listener, handler, peer_policy=SecondOnly())
        runtime.start()
        self.assertTrue(handled.wait(1))
        runtime.stop(join_timeout=2)
        self.assertEqual(seen, [1234])
        self.assertTrue(denied.closed)
        self.assertTrue(denied.stream.closed)
        self.assertEqual(denied.stream.outgoing.getvalue(), b"")
        self.assertIsNone(runtime.error)
        self.assertEqual(calls["close"], 1)

    def test_peer_policy_exception_is_a_runtime_failure(self) -> None:
        client = FakeClient(os.getuid())
        listener = FakeListener((client,))

        class Explode:
            def admit(self, connection: Connection) -> bool:
                raise RuntimeError("private-policy-marker")

        runtime, _ = make_runtime(listener, peer_policy=Explode())
        runtime.start()
        self.assertTrue(runtime.wait_failed(1))
        with self.assertRaises(RuntimeError_) as raised:
            runtime.stop(join_timeout=2)
        self.assertEqual(str(raised.exception), "test broker failed")
        self.assertTrue(client.closed)
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_broker_peer tests.container.test_broker_runtime -v 2>&1 | tail -8`
Expected: `ModuleNotFoundError: agent_container.broker.peer`; runtime tests error on `Connection` arity.

- [ ] **Step 3: Implement**

Create `src/agent_container/broker/peer.py`:

```python
"""Per-connection peer admission shared by every broker runtime."""

import os
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from agent_container.broker.runtime import Connection


class PeerPolicy(Protocol):
    """True admits the connection; False closes it unread; raise to fail the runtime."""

    def admit(self, connection: "Connection") -> bool: ...


class SameUser:
    def admit(self, connection: "Connection") -> bool:
        return connection.peer_uid == os.getuid()
```

In `src/agent_container/broker/runtime.py`:

```python
from agent_container.broker.peer import PeerPolicy


@dataclass(frozen=True)
class Connection:
    client: Any
    stream: Any
    peer_uid: int
    peer_pid: int
    peer_gid: int


def open_connection(client: Any, *, timeout: float) -> Connection:
    client.settimeout(timeout)
    credentials = client.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        _PEER_CREDENTIAL_BYTES,
    )
    peer_pid, peer_uid, peer_gid = struct.unpack("3i", credentials)
    return Connection(
        client, client.makefile("rwb", buffering=0), peer_uid, peer_pid, peer_gid
    )


def admit_connection(
    client: Any, *, timeout: float, policy: PeerPolicy | None
) -> Connection | None:
    connection = open_connection(client, timeout=timeout)
    if policy is None:
        return connection
    try:
        admitted = policy.admit(connection)
    except BaseException:
        connection.stream.close()
        raise
    if not admitted:
        connection.stream.close()
        return None
    return connection
```

Add the field `peer_policy: PeerPolicy | None = None` to `SocketBrokerRuntime` right after `deactivate_after_join: bool = False`, and change `_handle_client`:

```python
    def _handle_client(self, client: Any) -> None:
        if self.raw_client:
            self.handler(client)
            return
        connection = admit_connection(
            client, timeout=self.client_timeout, policy=self.peer_policy
        )
        if connection is None:
            return
        try:
            self.handler(connection)
        finally:
            connection.stream.close()
```

- [ ] **Step 4: Run kernel, handover, egress runtime tests**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_broker_peer tests.container.test_broker_runtime tests.container.test_broker_worker_start_stop tests.container.test_handover_broker_runtime tests.container.test_egress_broker_runtime -v 2>&1 | tail -5`
Expected: all PASS (egress uses `open_connection` with `raw_client=True`; handover passes no policy).

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/broker/peer.py src/agent_container/broker/runtime.py tests/container/test_broker_peer.py tests/container/test_broker_runtime.py
git commit -m "feat: keep peer pid/gid and add per-connection peer policy (K3)"
```

---

### Task 4: K1 fail-closed stop when `deactivate()` raises (#98)

**Files:**
- Modify: `src/agent_container/broker/runtime.py` (`SocketBrokerRuntime.stop`)
- Modify: `tests/container/test_broker_runtime.py` (`make_runtime(deactivate=)`)
- Create: `tests/container/test_broker_deactivate_failure.py`

**Interfaces:**
- Produces: `SocketBrokerRuntime.deactivate_error: BaseException | None`; `stop()` message `f"{label} deactivate failed"`; priority did not stop → deactivate failed → cleanup failed → failed; `exited` stays `False` after a deactivate failure even when `close()` succeeded.
- `make_runtime(..., deactivate=None)`: when given, replaces the counting default; the caller counts calls itself.

- [ ] **Step 1: Extend `make_runtime` and write the failing tests**

In `tests/container/test_broker_runtime.py` change the `make_runtime` signature to `def make_runtime(listener, handler=None, *, readiness=None, open_listener=None, close=None, deactivate=None, **options)`, rename the inner `deactivate` to `default_deactivate`, and pass `deactivate=deactivate or default_deactivate`.

Create `tests/container/test_broker_deactivate_failure.py`:

```python
"""K1: a failing deactivate() still closes the listener, joins, and cleans up."""

import os
import threading
import unittest
from unittest import mock

from tests.container.test_broker_runtime import FakeClient
from tests.container.test_broker_runtime import FakeListener
from tests.container.test_broker_runtime import RuntimeError_
from tests.container.test_broker_runtime import make_runtime


class FlakyDeactivate:
    def __init__(self, failures: int = 1) -> None:
        self.calls = 0
        self.failures = failures

    def __call__(self) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise ValueError("private-deactivate-marker")


class DeactivateFailureTest(unittest.TestCase):
    def test_failure_continues_cleanup_and_is_reported_until_deactivate_succeeds(self) -> None:
        for late, concurrency in (
            (False, "inline"),
            (True, "inline"),
            (False, "thread"),
            (True, "thread"),
        ):
            with self.subTest(deactivate_after_join=late, concurrency=concurrency):
                handled = threading.Event()
                client = FakeClient(os.getuid())
                listener = FakeListener((client,))
                deactivate = FlakyDeactivate()

                def handler(connection) -> int:
                    handled.set()
                    return 0

                runtime, calls = make_runtime(
                    listener,
                    handler,
                    deactivate=deactivate,
                    concurrency=concurrency,
                    raw_client=concurrency == "thread",
                    deactivate_after_join=late,
                )
                runtime.start()
                self.assertTrue(handled.wait(1))

                with self.assertRaises(RuntimeError_) as raised:
                    runtime.stop(join_timeout=2)
                self.assertEqual(str(raised.exception), "test broker deactivate failed")
                self.assertNotIn("private-deactivate-marker", str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertTrue(listener.closed)
                self.assertTrue(client.closed)
                self.assertEqual(deactivate.calls, 1)
                self.assertEqual(calls["close"], 1)
                self.assertFalse(runtime.exited)
                self.assertIsInstance(runtime.deactivate_error, ValueError)

                runtime.stop(join_timeout=2)
                self.assertEqual(deactivate.calls, 2)
                self.assertEqual(calls["close"], 2)
                self.assertTrue(runtime.exited)
                self.assertIsNone(runtime.deactivate_error)

                runtime.stop(join_timeout=2)
                self.assertEqual(calls["close"], 2)

    def test_did_not_stop_outranks_deactivate_failure_and_retry_deactivates_again(self) -> None:
        listener = FakeListener()
        deactivate = FlakyDeactivate()

        class StuckThread:
            daemon = True

            def __init__(self, **_: object) -> None:
                self.alive = True

            def start(self) -> None:
                pass

            def join(self, timeout: float) -> None:
                pass

            def is_alive(self) -> bool:
                return self.alive

        thread = StuckThread()
        runtime, calls = make_runtime(listener, deactivate=deactivate)
        with mock.patch("threading.Thread", return_value=thread):
            runtime.start()
        with self.assertRaises(RuntimeError_) as raised:
            runtime.stop(join_timeout=0)
        self.assertEqual(str(raised.exception), "test broker did not stop")
        self.assertEqual(deactivate.calls, 1)
        self.assertEqual(calls["close"], 0)
        self.assertTrue(listener.closed)

        thread.alive = False
        runtime.stop(join_timeout=0)
        self.assertEqual(deactivate.calls, 2)
        self.assertEqual(calls["close"], 1)
        self.assertTrue(runtime.exited)

    def test_deactivate_failure_outranks_cleanup_failure(self) -> None:
        listener = FakeListener()
        deactivate = FlakyDeactivate(failures=99)

        def failing_close() -> None:
            raise OSError("private-close-marker")

        runtime, _ = make_runtime(listener, deactivate=deactivate, close=failing_close)
        runtime.start()
        with self.assertRaises(RuntimeError_) as raised:
            runtime.stop(join_timeout=2)
        self.assertEqual(str(raised.exception), "test broker deactivate failed")
        self.assertFalse(runtime.exited)

    def test_base_exception_from_deactivate_is_not_swallowed(self) -> None:
        listener = FakeListener()

        def interrupt() -> None:
            raise KeyboardInterrupt

        runtime, calls = make_runtime(listener, deactivate=interrupt)
        runtime.start()
        with self.assertRaises(KeyboardInterrupt):
            runtime.stop(join_timeout=2)
        self.assertFalse(runtime.exited)
        runtime.deactivate = lambda: None
        runtime.stop(join_timeout=2)
        self.assertEqual(calls["close"], 1)
        self.assertTrue(runtime.exited)
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_broker_deactivate_failure -v 2>&1 | tail -8`
Expected: FAIL — the raw `ValueError("private-deactivate-marker")` escapes `stop()`.

- [ ] **Step 3: Implement the stop contract**

In `SocketBrokerRuntime` add the field `deactivate_error: BaseException | None = field(default=None, init=False, repr=False)` next to `error`, and:

```python
    def _try_deactivate(self) -> bool:
        try:
            self.deactivate()
        except Exception as error:
            self.deactivate_error = error
            return False
        self.deactivate_error = None
        return True

    def stop(self, *, join_timeout: float) -> None:
        if self.exited:
            return
        self.stop_event.set()
        deactivate_failed = False
        if not self.deactivate_after_join:
            deactivate_failed = not self._try_deactivate()
        cleanup_failed = False
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                cleanup_failed = True
            else:
                self.listener = None

        did_not_stop = False
        if self.thread is not None:
            self.thread.join(timeout=join_timeout)
            did_not_stop = self.thread.is_alive()
        if self._join_workers(join_timeout):
            did_not_stop = True
        if self.deactivate_after_join:
            deactivate_failed = not self._try_deactivate()

        if did_not_stop:
            raise self.error_type(f"{self.label} did not stop") from None

        # Cleanup runs even after a failed deactivate: removing the socket and
        # capability shrinks the exposed surface. The runtime still does not
        # count as exited until deactivate has succeeded (fail-closed).
        try:
            self.close()
        except (OSError, ValueError):
            cleanup_failed = True
        else:
            if not deactivate_failed:
                self.exited = True

        if deactivate_failed:
            raise self.error_type(f"{self.label} deactivate failed") from None
        if cleanup_failed:
            raise self.error_type(f"{self.label} cleanup failed") from None
        if self.error is not None:
            raise self.error_type(f"{self.label} failed") from None
```

- [ ] **Step 4: Run kernel runtime tests**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_broker_deactivate_failure tests.container.test_broker_runtime tests.container.test_broker_worker_start_stop tests.container.test_handover_broker_runtime tests.container.test_egress_broker_runtime -v 2>&1 | tail -5`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/broker/runtime.py tests/container/test_broker_runtime.py tests/container/test_broker_deactivate_failure.py
git commit -m "fix: keep broker stop fail-closed when deactivate raises (K1, #98)"
```

---

### Task 5: K2 `RuntimeArtifacts`

**Files:**
- Create: `src/agent_container/broker/artifacts.py`
- Create: `tests/container/test_broker_artifacts.py`

**Interfaces:**
- Produces: `RuntimeArtifacts.open(run_dir: Path, *, label: str) -> RuntimeArtifacts`; `track_file(name: str) -> None`; `track_socket(name: str) -> None`; `remove() -> bool` (True = failed); `close() -> None`.
- `open` raises `PermissionError(f"{label} run directory is not private")` unless the directory is mode `0700` and owned by the running user. `track_*` raises `ValueError(f"{label} artifact type is invalid")` when the name exists with the wrong type; a missing name is recorded without identity. `remove` order: tracked files, then tracked sockets, then `rmdir`.

- [ ] **Step 1: Write the failing tests**

Create `tests/container/test_broker_artifacts.py`:

```python
import os
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest import mock

from agent_container.broker.artifacts import RuntimeArtifacts
from agent_container.broker.runtime import bind_private_listener


LABEL = "test broker"


def _run_dir(root: Path) -> Path:
    run_dir = root / "run"
    run_dir.mkdir(mode=0o700)
    return run_dir


def _capability(run_dir: Path) -> Path:
    path = run_dir / "capability"
    path.write_text("c" * 43 + "\n", encoding="ascii")
    path.chmod(0o600)
    return path


def _bind(run_dir: Path) -> Path:
    path = run_dir / "broker.sock"
    bind_private_listener(path, backlog=1, label=LABEL).close()
    return path


def _tracked(run_dir: Path) -> RuntimeArtifacts:
    artifacts = RuntimeArtifacts.open(run_dir, label=LABEL)
    artifacts.track_file("capability")
    artifacts.track_socket("broker.sock")
    return artifacts


class RuntimeArtifactsTest(unittest.TestCase):
    def test_removes_file_then_socket_then_directory_and_closes_descriptor(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = _run_dir(Path(directory))
            _capability(run_dir)
            _bind(run_dir)
            artifacts = _tracked(run_dir)
            order: list[str] = []
            real_unlink = os.unlink

            def recording_unlink(name, *args, **kwargs):
                order.append(name)
                return real_unlink(name, *args, **kwargs)

            with mock.patch("os.unlink", side_effect=recording_unlink):
                self.assertFalse(artifacts.remove())
            self.assertEqual(order, ["capability", "broker.sock"])
            self.assertFalse(run_dir.exists())
            with self.assertRaises(OSError):
                os.fstat(artifacts._descriptor)
            self.assertFalse(artifacts.remove())

    def test_missing_artifacts_are_not_failures(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = _run_dir(Path(directory))
            artifacts = _tracked(run_dir)
            self.assertFalse(artifacts.remove())
            self.assertFalse(run_dir.exists())

    def test_open_requires_a_private_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = Path(directory) / "shared"
            run_dir.mkdir(mode=0o750)
            with self.assertRaisesRegex(PermissionError, "test broker run directory is not private"):
                RuntimeArtifacts.open(run_dir, label=LABEL)
            link = Path(directory) / "link"
            link.symlink_to(_run_dir(Path(directory)))
            with self.assertRaises(OSError):
                RuntimeArtifacts.open(link, label=LABEL)

    def test_track_rejects_a_name_of_the_wrong_type(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = _run_dir(Path(directory))
            _capability(run_dir)
            artifacts = RuntimeArtifacts.open(run_dir, label=LABEL)
            with self.assertRaisesRegex(ValueError, "test broker artifact type is invalid"):
                artifacts.track_socket("capability")
            artifacts.close()

    def test_replaced_file_is_kept_and_reported(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = _run_dir(Path(directory))
            capability = _capability(run_dir)
            _bind(run_dir)
            artifacts = _tracked(run_dir)
            capability.unlink()
            capability.mkdir()
            self.assertTrue(artifacts.remove())
            self.assertTrue(capability.is_dir())
            self.assertFalse((run_dir / "broker.sock").exists())
            self.assertTrue(run_dir.exists())
            capability.rmdir()
            self.assertFalse(artifacts.remove())
            self.assertFalse(run_dir.exists())

    def test_replaced_socket_inode_is_kept_even_when_it_is_a_socket(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = _run_dir(Path(directory))
            _capability(run_dir)
            socket_path = _bind(run_dir)
            artifacts = _tracked(run_dir)
            socket_path.unlink()
            _bind(run_dir)
            self.assertTrue(artifacts.remove())
            self.assertTrue(stat.S_ISSOCK(socket_path.lstat().st_mode))
            self.assertFalse((run_dir / "capability").exists())
            self.assertTrue(run_dir.exists())

    def test_name_that_appears_after_tracking_is_foreign(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = _run_dir(Path(directory))
            artifacts = _tracked(run_dir)
            (run_dir / "broker.sock").write_text("replacement", encoding="ascii")
            self.assertTrue(artifacts.remove())
            self.assertTrue((run_dir / "broker.sock").is_file())
            (run_dir / "broker.sock").unlink()
            self.assertFalse(artifacts.remove())
            self.assertFalse(run_dir.exists())

    def test_replaced_run_directory_is_kept(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = _run_dir(Path(directory))
            artifacts = _tracked(run_dir)
            moved = Path(directory) / "moved"
            run_dir.rename(moved)
            run_dir.mkdir(mode=0o700)
            self.assertTrue(artifacts.remove())
            self.assertTrue(run_dir.is_dir())
            self.assertTrue(moved.is_dir())
            artifacts.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_broker_artifacts -v 2>&1 | tail -5`
Expected: `ModuleNotFoundError: agent_container.broker.artifacts`.

- [ ] **Step 3: Implement `RuntimeArtifacts`**

Create `src/agent_container/broker/artifacts.py`:

```python
"""Identity-checked removal of a broker run directory and its artifacts."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
from typing import Callable


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | os.O_DIRECTORY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
Identity = tuple[int, int]


@dataclass(frozen=True)
class _Tracked:
    name: str
    expected_type: Callable[[int], bool]
    identity: Identity | None


@dataclass
class RuntimeArtifacts:
    run_dir: Path
    label: str
    _descriptor: int = field(default=-1, repr=False)
    _directory_identity: Identity | None = field(default=None, repr=False)
    _files: list[_Tracked] = field(default_factory=list, repr=False)
    _sockets: list[_Tracked] = field(default_factory=list, repr=False)
    _removed: bool = field(default=False, repr=False)

    @classmethod
    def open(cls, run_dir: Path, *, label: str) -> "RuntimeArtifacts":
        descriptor = os.open(run_dir, _DIRECTORY_FLAGS)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid != os.getuid()
            ):
                raise PermissionError(f"{label} run directory is not private")
        except BaseException:
            os.close(descriptor)
            raise
        return cls(
            run_dir=run_dir,
            label=label,
            _descriptor=descriptor,
            _directory_identity=(metadata.st_dev, metadata.st_ino),
        )

    def _require_open(self) -> None:
        if self._descriptor < 0:
            raise ValueError(f"{self.label} run directory is closed")

    def _track(
        self, name: str, expected_type: Callable[[int], bool], into: list[_Tracked]
    ) -> None:
        self._require_open()
        try:
            metadata = os.stat(name, dir_fd=self._descriptor, follow_symlinks=False)
        except FileNotFoundError:
            identity: Identity | None = None
        else:
            if not expected_type(metadata.st_mode):
                raise ValueError(f"{self.label} artifact type is invalid")
            identity = (metadata.st_dev, metadata.st_ino)
        into.append(_Tracked(name, expected_type, identity))

    def track_file(self, name: str) -> None:
        self._track(name, stat.S_ISREG, self._files)

    def track_socket(self, name: str) -> None:
        self._track(name, stat.S_ISSOCK, self._sockets)

    def _remove_entry(self, tracked: _Tracked) -> bool:
        try:
            metadata = os.stat(
                tracked.name, dir_fd=self._descriptor, follow_symlinks=False
            )
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if (
            tracked.identity is None
            or not tracked.expected_type(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != tracked.identity
        ):
            return False
        try:
            os.unlink(tracked.name, dir_fd=self._descriptor)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def _remove_directory(self) -> bool:
        try:
            current = self.run_dir.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != self._directory_identity
        ):
            return False
        try:
            self.run_dir.rmdir()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def remove(self) -> bool:
        """Remove tracked files, then sockets, then the directory. True means failed.

        A failed removal keeps the descriptor so the caller can retry once the
        foreign entry has been dealt with; a successful one closes it.
        """
        if self._removed:
            return False
        self._require_open()
        failed = False
        for tracked in (*self._files, *self._sockets):
            if not self._remove_entry(tracked):
                failed = True
        if not self._remove_directory():
            failed = True
        if failed:
            return True
        self._removed = True
        self.close()
        return False

    def close(self) -> None:
        if self._descriptor >= 0:
            descriptor = self._descriptor
            self._descriptor = -1
            os.close(descriptor)
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_broker_artifacts -v 2>&1 | tail -5`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/broker/artifacts.py tests/container/test_broker_artifacts.py
git commit -m "feat: add identity-checked RuntimeArtifacts cleanup (K2)"
```

---

### Task 6: handover session on `RuntimeArtifacts` and the K1 contract (H2, H3, H4)

**Files:**
- Modify: `src/agent_container/handover_broker.py`
- Modify: `tests/container/test_handover_broker.py`
- Modify: `tests/container/test_handover_broker_runtime.py`

**Interfaces:**
- Consumes: `RuntimeArtifacts` (Task 5), `SocketBrokerRuntime` K1 contract (Task 4).
- Produces: `HandoverBrokerSession._artifacts: RuntimeArtifacts`; `close()` unchanged in name, message `handover broker cleanup failed`, retryable after removing a foreign entry.

- [ ] **Step 1: Write the failing tests**

In `tests/container/test_handover_broker.py` add to `HandoverBrokerSessionTest` (tag: H2):

```python
    def test_close_keeps_a_replaced_socket_inode_even_when_it_is_a_socket(self) -> None:
        run_dir = self.session.run_dir
        listener = self.session.open_listener()
        listener.close()
        self.session.socket_path.unlink()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        replacement.bind(str(self.session.socket_path))
        replacement.close()

        with self.assertRaisesRegex(ValueError, "cleanup failed"):
            self.session.close()
        self.assertTrue(stat.S_ISSOCK(self.session.socket_path.lstat().st_mode))
        self.assertFalse(self.session.capability_path.exists())
        self.assertTrue(run_dir.exists())

        self.session.socket_path.unlink()
        self.session.close()
        self.assertFalse(run_dir.exists())
```

In `tests/container/test_handover_broker_runtime.py` add (tag: H3). The module has no shared `setUp`; build the real session the way `test_authorized_delayed_operation_cannot_publish_after_exit_timeout` does:

```python
    def test_deactivate_failure_still_removes_runtime_and_reports_fixed_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hb-runtime-") as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir(mode=0o700)
            handovers = root / "handovers"
            handovers.mkdir(mode=0o700)
            project = handovers / "agent-container"
            project.mkdir(mode=0o700)
            session = HandoverBrokerSession.create(
                state.resolve(),
                "agent-container",
                project.resolve(),
            )
            calls = {"deactivate": 0}
            real_deactivate = session.deactivate

            def flaky_deactivate() -> None:
                calls["deactivate"] += 1
                if calls["deactivate"] == 1:
                    raise ValueError("private-deactivate-marker")
                real_deactivate()

            run_dir = session.run_dir
            runtime = HandoverBrokerRuntime(session)
            with mock.patch.object(session, "deactivate", flaky_deactivate):
                runtime.__enter__()
                with self.assertRaises(HandoverBrokerRuntimeError) as raised:
                    runtime.__exit__(None, None, None)
                self.assertEqual(
                    str(raised.exception), "handover broker deactivate failed"
                )
                self.assertNotIn("private-deactivate-marker", str(raised.exception))
                self.assertFalse(run_dir.exists())
                self.assertFalse(runtime._runtime.exited)
                runtime.__exit__(None, None, None)
            self.assertTrue(runtime._runtime.exited)
            self.assertEqual(calls["deactivate"], 3)
            self.assertEqual(session._capability, "")
```

(`session.close()` calls `self.deactivate()`, so the first `__exit__` makes three calls in total across both exits: the failing one, the one inside `close()`, and the retry's own.)

(`session.close()` calls `self.deactivate()`, so the second call inside `close` succeeds and the run directory is removed during the first `__exit__`.)

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker tests.container.test_handover_broker_runtime -v 2>&1 | tail -8`
Expected: the H2 test FAILS (kernel `remove_runtime_artifacts` unlinks the replacement socket); the H3 test PASSES already if Task 4 landed — keep it as the broker-level pin.

- [ ] **Step 3: Move the session onto `RuntimeArtifacts`**

In `src/agent_container/handover_broker.py`:

- Replace `from agent_container.broker.runtime import remove_runtime_artifacts` with `from agent_container.broker.artifacts import RuntimeArtifacts`.
- Add the dataclass field `_artifacts: RuntimeArtifacts = field(repr=False)` immediately after `_capability: str = field(repr=False)`.
- In `create`, after the capability file is written:

```python
        try:
            artifacts = RuntimeArtifacts.open(run_dir, label=_LABEL)
        except Exception:
            shutil.rmtree(run_dir)
            raise
        try:
            artifacts.track_file("capability")
        except Exception:
            artifacts.close()
            shutil.rmtree(run_dir)
            raise
        return cls(
            ...,
            _capability=capability,
            _artifacts=artifacts,
        )
```

- In `open_listener`, after `listener = bind_private_listener(...)`:

```python
        try:
            self._artifacts.track_socket("broker.sock")
        except Exception:
            listener.close()
            raise
        self._listener = listener
        return listener
```

- In `close`, replace the `remove_runtime_artifacts(...)` block with `if self._artifacts.remove(): cleanup_failed = True`.

- [ ] **Step 4: Run handover tests, goldens, socket integration**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker tests.container.test_handover_broker_runtime tests.container.test_handover_broker_transport tests.container.test_broker_audit_golden tests.container.test_broker_frame_golden -v 2>&1 | tail -5`
Then: `AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.integration.test_handover_broker_socket -v 2>&1 | tail -5`
Expected: all PASS, no `ResourceWarning`. The existing replaced-path tests (`test_close_refuses_replaced_socket_path`, `test_close_refuses_replaced_capability_path`) pass unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/handover_broker.py tests/container/test_handover_broker.py tests/container/test_handover_broker_runtime.py
git commit -m "refactor: clean handover runtime artifacts by identity (H2, H3)"
```

---

### Task 7: handover transport on kernel `read_frame` (H1)

**Files:**
- Modify: `src/agent_container/handover_broker_transport.py`
- Test: `tests/container/test_handover_broker_transport.py` (no edits expected)

**Interfaces:**
- Consumes: `FrameSizeError` (Task 1), `read_request_frame` from `handover_broker_protocol`.
- Produces: `_read_one_request(connection) -> HandoverRequest` raising `_RequestFailure("size")` for `FrameSizeError` and `_RequestFailure("schema")` for any other `ValueError`.

- [ ] **Step 1: Confirm the transport tests pin the size／schema split**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker_transport -v 2>&1 | grep -c "ok$"`
Expected: a positive count; note it. `test_fixed_failures_create_no_file_and_leak_no_request_or_error_text` covers the `size` header-only case and the malformed JSON `schema` case.

- [ ] **Step 2: Replace the private readers**

In `src/agent_container/handover_broker_transport.py`:

- Delete `_read_exact` and the `import struct`, `MAX_REQUEST_BYTES`, `decode_request_frame` imports.
- Add `from agent_container.broker.frame import FrameSizeError` and `from agent_container.handover_broker_protocol import read_request_frame`.
- Replace `_read_one_request` with:

```python
def _read_one_request(connection: BinaryIO) -> HandoverRequest:
    try:
        return read_request_frame(connection)
    except FrameSizeError:
        raise _RequestFailure("size") from None
    except ValueError:
        raise _RequestFailure("schema") from None
```

- [ ] **Step 3: Run the transport tests unchanged**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_handover_broker_transport tests.container.test_handover_broker_client -v 2>&1 | tail -5`
Expected: same count of `ok` as Step 1, no edits to the test file. If a test needed editing, stop: that is an untagged behavior change.

- [ ] **Step 4: Commit**

```bash
git add src/agent_container/handover_broker_transport.py
git commit -m "refactor: read handover requests through the kernel frame reader (H1)"
```

---

### Task 8: egress session on `RuntimeArtifacts`, K1 pin, delete `remove_runtime_artifacts` (E5, E6)

**Files:**
- Modify: `src/agent_container/egress_broker.py`
- Modify: `src/agent_container/broker/runtime.py` (delete `remove_runtime_artifacts`)
- Modify: `tests/container/test_egress_broker.py`, `tests/container/test_egress_broker_runtime.py`, `tests/container/test_broker_runtime.py`

**Interfaces:**
- Consumes: `RuntimeArtifacts`, K1 contract.
- Produces: `EgressBrokerSession._artifacts`; `remove_runtime_artifacts` no longer exists anywhere (`grep -rn remove_runtime_artifacts src tests` is empty).

- [ ] **Step 1: Write the failing tests**

In `tests/container/test_egress_broker.py` add (tag: E5; `socket` and `stat` imports as needed):

```python
    def test_close_keeps_a_replaced_socket_inode_even_when_it_is_a_socket(self) -> None:
        run_dir = self.session.run_dir
        listener = self.session.open_listener()
        listener.close()
        self.session.socket_path.unlink()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        replacement.bind(str(self.session.socket_path))
        replacement.close()

        with self.assertRaisesRegex(ValueError, "cleanup failed"):
            self.session.close()
        self.assertTrue(stat.S_ISSOCK(self.session.socket_path.lstat().st_mode))
        self.assertFalse(self.session.capability_path.exists())
        self.assertTrue(run_dir.exists())

        self.session.socket_path.unlink()
        self.session.close()
        self.assertFalse(run_dir.exists())
```

In `tests/container/test_egress_broker_runtime.py` add to `EgressBrokerRuntimeTest` (tag: E6), using the module's `FakeSession`／`FakeListener`:

```python
    def test_deactivate_failure_after_join_still_closes_and_reports_fixed_error(self) -> None:
        class FlakySession(FakeSession):
            def deactivate(self) -> None:
                super().deactivate()
                if self.deactivate_calls == 1:
                    raise ValueError("private-deactivate-marker")

        listener = FakeListener()
        session = FlakySession(listener)
        runtime = EgressBrokerRuntime(session)  # type: ignore[arg-type]
        runtime.__enter__()
        with self.assertRaises(EgressBrokerRuntimeError) as raised:
            runtime.__exit__(None, None, None)
        self.assertEqual(str(raised.exception), "egress broker deactivate failed")
        self.assertNotIn("private-deactivate-marker", str(raised.exception))
        self.assertTrue(listener.closed)
        self.assertEqual(session.deactivate_calls, 1)
        self.assertEqual(session.close_calls, 1)
        self.assertFalse(runtime._runtime.exited)

        runtime.__exit__(None, None, None)
        self.assertEqual(session.deactivate_calls, 2)
        self.assertEqual(session.close_calls, 2)
        self.assertTrue(runtime._runtime.exited)
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_egress_broker tests.container.test_egress_broker_runtime -v 2>&1 | tail -8`
Expected: the E5 test FAILS (replacement socket unlinked); E6 passes on the Task 4 kernel and stays as the broker-level pin.

- [ ] **Step 3: Move the egress session onto `RuntimeArtifacts`**

In `src/agent_container/egress_broker.py` apply exactly the same edit shape as Task 6 Step 3: import `RuntimeArtifacts` instead of `remove_runtime_artifacts`; field `_artifacts: RuntimeArtifacts = field(repr=False)` after `_capability`; in `create` open the artifacts and `track_file("capability")` after `create_private_file(...)` with `shutil.rmtree(run_dir)` on failure (close the artifacts first when `track_file` fails); in `open_listener` call `self._artifacts.track_socket("broker.sock")` after `bind_private_listener` and close the listener on failure; in `close` replace the `remove_runtime_artifacts(...)` block with `if self._artifacts.remove(): cleanup_failed = True`.

- [ ] **Step 4: Delete `remove_runtime_artifacts`**

- Remove the function from `src/agent_container/broker/runtime.py` (L93-121 today) and its import in `tests/container/test_broker_runtime.py`.
- Delete `RemoveRuntimeArtifactsTest` (L155-206 today) from `tests/container/test_broker_runtime.py`; its cases live in `tests/container/test_broker_artifacts.py` (tag: K2).
- Verify: `grep -rn "remove_runtime_artifacts" src tests` prints nothing.

- [ ] **Step 5: Run egress tests, goldens, socket integration**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_egress_broker tests.container.test_egress_broker_runtime tests.container.test_egress_adapter tests.container.test_broker_egress_golden tests.container.test_broker_runtime -v 2>&1 | tail -5`
Then: `AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.integration.test_egress_broker_socket -v 2>&1 | tail -5`
Expected: all PASS; the egress golden's capability mode assertion (`0o400`) is untouched in this PR (E1 belongs to S2-3).

- [ ] **Step 6: Commit**

```bash
git add src/agent_container/egress_broker.py src/agent_container/broker/runtime.py tests/container/test_egress_broker.py tests/container/test_egress_broker_runtime.py tests/container/test_broker_runtime.py
git commit -m "refactor: clean egress runtime artifacts by identity and drop remove_runtime_artifacts (E5, E6, K2)"
```

---

### Task 9: CHANGELOG, full verification, PR evidence

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add the Unreleased entries**

Under `## [Unreleased]` → `### Added`, first bullet:

```markdown
- Phase 6 stage 2の最初のPR（S2-1）として、共通broker kernelに保証を追加しました。frame errorを種別ごとの`FrameError` subclassにし（message不変）、`AuditLog.append`が共通key（`timestamp`／`run`／`project`／`operation`／`status`、任意の`stage`）を検証し、`Connection`がpeerのpid／gidを保持して接続毎の`PeerPolicy`（既定実装`SameUser`）で入口を絞れるようにし、run directoryのdir_fdとinode identityで差し替えを温存する`RuntimeArtifacts`を追加しました。handoverとegressのsessionは`RuntimeArtifacts`で片付け、handover transportはkernelのframe readerで`size`／`schema`のaudit stageを保存します。設計は[`docs/superpowers/specs/2026-09-06-broker-kernel-stage2-design.md`](docs/superpowers/specs/2026-09-06-broker-kernel-stage2-design.md)、意図的に変えた挙動はK1〜K5／H1〜H4／E5〜E6として同文書に列挙しています。
```

Under `### Fixed`, first bullet:

```markdown
- broker runtimeの`stop()`で`deactivate()`が例外を出すと、listener close・thread回収・cleanupへ進まずに例外が素通ししていました。失効失敗を捕捉してlistener close、accept／worker threadの回収、artifact除去まで継続し、固定文`<label> deactivate failed`で報告し、失効が成功するまで完了扱いにしないようにしました。優先順位はdid not stop → deactivate failed → cleanup failed → failedです（[#98](https://github.com/jj1xgo/agent-container/issues/98)）。
```

- [ ] **Step 2: Lint, full unit suites, goldens unchanged**

Run in order and record results:

```bash
bin/lint
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests/codex 2>&1 | tail -3
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests/container 2>&1 | tail -3
AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.integration.test_github_broker_socket tests.integration.test_handover_broker_socket tests.integration.test_egress_broker_socket 2>&1 | tail -3
git diff --check main...HEAD
git diff --stat main...HEAD -- tests/fixtures tests/container/test_broker_frame_golden.py tests/container/test_broker_audit_golden.py tests/container/test_broker_egress_golden.py tests/container/test_broker_github_golden.py tests/container/test_broker_family_golden.py
```

Expected: lint clean; codex 48 and container suites PASS with 0 failures; socket integration PASS with no `ResourceWarning`; the last `diff --stat` prints **nothing** (goldens untouched).

- [ ] **Step 3: Build the intentional-change list for the PR body**

Run: `git diff --stat main...HEAD -- tests/` and map every edited existing test file to its tag:

| file | tag |
| --- | --- |
| `tests/container/test_broker_frame.py` | K4 (added tests) |
| `tests/container/test_broker_audit.py` | K5 (records now carry the envelope) |
| `tests/container/test_broker_runtime.py` | K3 (`Connection` arity, `peer_policy` tests), K1 (`make_runtime(deactivate=)`), K2 (`RemoveRuntimeArtifactsTest` moved to `test_broker_artifacts.py`) |
| `tests/container/test_handover_broker.py` | H2 (added replaced-inode case) |
| `tests/container/test_handover_broker_runtime.py` | H3 (added deactivate-failure case) |
| `tests/container/test_egress_broker.py` | E5 (added replaced-inode case) |
| `tests/container/test_egress_broker_runtime.py` | E6 (added deactivate-failure case) |

Any other edited existing test file is an untagged change: stop and report before opening the PR.

- [ ] **Step 4: Commit and report**

```bash
git add CHANGELOG.md
git commit -m "docs: record stage 2 S2-1 kernel guarantees in the changelog"
```

Report: commit range, the verification results verbatim (counts, `not run` items with reasons — local Podman is `not run — podman unavailable`; stage 1 real-host smoke 6-6 is a **merge gate the user runs on `main` before this PR merges**), and the intentional-change table. Return `/workspace` to `main` (`git checkout main`) after the final commit.

---

## Self-review

- **Spec coverage.** K1 → Task 4; K2 → Tasks 5, 6, 8; K3 → Task 3; K4 → Task 1; K5 → Task 2; H1 → Task 7; H2／H3／H4 → Task 6 (H4 needs no code: handover records already carry the envelope, Task 2 Step 4 proves it); E5／E6 → Task 8; 「PR分割と受け入れ条件」 → Task 9. Not in this PR by design: E1〜E4 (S2-3), GitHub G1〜G8 (S2-2), Family F1〜F2 (S2-3), `append_text_record` removal (S2-2).
- **Placeholders.** None; every code step carries the code.
- **Type consistency.** `Connection(client, stream, peer_uid, peer_pid, peer_gid)` is used in that order in Tasks 3 and the runtime test; `FakeClient.getsockopt` packs `(1234, uid, 5678)` so `peer_pid == 1234`, `peer_gid == 5678`. `RuntimeArtifacts.open／track_file／track_socket／remove／close` names match between Tasks 5, 6, 8. `make_runtime(deactivate=)` is introduced in Task 4 before Task 4's tests use it.
