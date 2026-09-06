# Phase 6 stage 2 S2-2: GitHub broker full unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the GitHub broker's session lifecycle, cleanup, capability file, audit opener, frame/stream codec, and container-side capability reader onto the stage 2 kernel (spec items G1〜G8), replacing the stage 1 compatibility shims and the `test_github_broker_compatibility.py` pins with the kernel contracts.

**Architecture:** `BrokerSession` becomes the third consumer of `RuntimeArtifacts` / `AuditLog` / `create_private_file` / `bind_private_listener` in the exact shape `HandoverBrokerSession` already has, gaining `deactivate()` and a `_cleanup_complete` gate. `UploadPackBrokerRuntime` wraps a `SocketBrokerRuntime` the way `HandoverBrokerRuntime` does, with `peer_policy=SameUser()`. `github_broker_protocol.py` keeps GitHub's typed validation but delegates encoding, decoding, exact reads, and chunk writes to `broker/frame.py`. Container-side readers become one-line wrappers over `broker/capability.py`. Operation handlers, policy, audit record keys, `BrokerRuntimeMount`, and `podman.py` do not change.

**Tech Stack:** Python 3 standard library, `unittest`, `ruff` via `bin/lint`.

**Spec:** `docs/superpowers/specs/2026-09-06-broker-kernel-stage2-design.md` (K1〜K5, 「### GitHub」G1〜G8, 設計原則, PR分割と受け入れ条件). Facts: `docs/superpowers/plans/2026-09-06-broker-kernel-stage2-investigation.md` §3〜§4 and the S2-1 plan `docs/superpowers/plans/2026-09-06-broker-kernel-s2-1-kernel-guarantees.md` (kernel interfaces).

## Global Constraints

- Base: branch `feat/broker-kernel-stage2-1` (PR #117) or `main` after it merges. Kernel interfaces consumed: `RuntimeArtifacts.open(run_dir, *, label)` / `track_file(name)` / `track_socket(name)` / `remove() -> bool` (True = failed, retryable) / `close()`; `AuditLog(path, *, label)` with `validate()` / `append(record)` (envelope: `timestamp`, `run`, `project`, `operation`, `status ∈ {ok, denied, error}`, optional `stage`); `create_private_file(path, body, *, label, mode=0o600)`; `generate_capability(*, label)`; `bind_private_listener(socket_path, *, backlog, label)`; `SocketBrokerRuntime(label, thread_name, open_listener, handler, deactivate, close, error_type, peer_policy=..., backlog, listener_timeout, client_timeout)` with `start()` / `stop(join_timeout=)` and the K1 contract (did not stop → deactivate failed → cleanup failed → failed); `Connection(client, stream, peer_uid, peer_pid, peer_gid)`; `SameUser`; `FrameSchema` / `JsonOptions` / `encode_frame` / `decode_frame(..., json_decoder=)` / `read_exact(stream, size, *, label, initial_eof=False)` / `write_all` / `FrameError` subclasses (`FrameIncomplete`, `FrameSizeError`, `FrameJsonError`, `FrameSchemaError`, `StreamError`); `read_capability(path, *, label)` / `validate_socket(path, *, label)` / `validate_exact_path(path, *, label)`.
- Wire bytes unchanged: `tests/fixtures/broker_github_golden.json` and `tests/container/test_broker_github_golden.py` pass **without edits** (request frames, response frames, chunk framing, audit lines).
- Audit line bytes unchanged: record keys and order in `BrokerSession.audit` stay `timestamp, run, project, repository, operation, status, bytes, policy_version[, ref, pr_number, issue_number, stage]`.
- Kernel labels are chosen so existing messages survive: session label `"broker"` (→ `broker socket path is too long`, `broker socket path already exists`, `generated broker capability has invalid format`, `broker cleanup failed`, `broker run directory is not private`), audit label `"broker audit"` (→ `broker audit file must have mode 0600`, `broker audit file must be owned by the current user`), runtime label `"GitHub broker"` (→ `GitHub broker failed to start` / `did not stop` / `deactivate failed` / `cleanup failed` / `failed`; `agentctl` already prints `error: GitHub broker failed` for `GitHubBrokerRuntimeError`).
- Intentional behavior changes are exactly the G1〜G8 rows of the spec's GitHub table. Every edited existing test is listed in the PR body with its item number. An edit that cannot be tagged means behavior changed unintentionally: stop and report.
- `github_broker.py` keeps `import os` and `import socket` (tests patch `agent_container.github_broker.socket.socket` / `os.chmod`; those patch the shared module objects, so they keep intercepting `bind_private_listener`).
- `UploadPackBrokerRuntime.create(layout, record)` keeps its name, signature, and return type (nine `test_agentctl.py` sites mock it wholesale).
- Family (`family_*`) and egress/handover sources are not touched. `podman.py`, `BrokerRuntimeMount`, `github_broker_policy.py`, operation handlers in `github_broker_transport.py` (other than the three reader wrappers) are not touched.
- Local commands: `bin/lint`; `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest <module>`; full container suite `... discover -s tests/container`; socket integration `AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.integration.test_github_broker_socket tests.integration.test_handover_broker_socket tests.integration.test_egress_broker_socket`. Run the container suite once with `TMPDIR` on an ext4 directory outside the repo tree as well as on tmpfs (inode-reuse regressions only show on ext4).
- Sandbox notes: `.git/worktrees` and `.github` are read-only; work on a branch checked out in `/workspace`; never stage `.github/workflows/ci.yml`; in the sandbox `test_agent_handover_wrapper...test_create_uses_fixed_environment_scope_and_session` and the `test_docs` CI-file test fail for environment reasons (CI is the arbiter).

---

## File structure

| File | Responsibility after S2-2 |
| --- | --- |
| `src/agent_container/github_broker.py` (modify) | `BrokerSession`: policy/authorize/audit record policy only; artifacts, capability file, listener bind, audit opener from the kernel; `deactivate()`; retryable `close()` |
| `src/agent_container/github_broker_runtime.py` (modify) | `UploadPackBrokerRuntime` = policy/transport wiring + `SocketBrokerRuntime` |
| `src/agent_container/github_broker_protocol.py` (modify) | GitHub typed validation over kernel codec; no private frame/stream code |
| `src/agent_container/github_broker_transport.py` (modify) | reader wrappers delegate to `broker/capability.py`; handlers unchanged |
| `src/agent_container/broker/frame.py` (modify) | `write_chunk_stream` writes through `write_all` (G6) |
| `tests/container/test_github_broker.py` (modify) | G5 audit-file assertion; G4 replaced-inode and retry cases; `_AUDIT_STATUSES` narrowing |
| `tests/container/test_github_broker_compatibility.py` (rewrite) | pins the new kernel-backed contracts instead of the stage 1 quirks |
| `tests/container/test_github_broker_runtime.py` (modify) | G1 lifecycle tests (start failure, stop order, deactivate failure, peer denial) |
| `tests/container/test_broker_frame.py` (modify) | G6 chunk writer short-write test |
| `tests/container/test_github_broker_transport.py` (modify, additions only) | G7 reader tests for size-exact and identity re-check |
| `CHANGELOG.md` (modify) | Unreleased entry for S2-2 |

---

### Task 1: `BrokerSession` on the kernel (G2, G3, G4, G5, K1 deactivate split)

**Files:**
- Modify: `src/agent_container/github_broker.py`
- Modify: `tests/container/test_github_broker.py`

**Interfaces:**
- Consumes: `RuntimeArtifacts`, `AuditLog`, `create_private_file`, `generate_capability`, `bind_private_listener`, `allocate_run_dir`.
- Produces: `BrokerSession.deactivate() -> None` (lock-guarded `_closed=True`, `_capability=""`), `BrokerSession.close() -> None` (idempotent after success via `_cleanup_complete`; raises `ValueError("broker cleanup failed")` and stays retryable on failure), `BrokerSession._artifacts: RuntimeArtifacts`, `_AUDIT_STATUSES = frozenset({"ok", "denied", "error"})`.

- [ ] **Step 1: Write the failing tests**

Edit `tests/container/test_github_broker.py`:

1. In `test_audit_rejects_unvalidated_metadata_without_writing`, replace the final assertion `self.assertFalse(self.session.audit_file.exists())` with (tag G5):

```python
        self.assertEqual(self.session.audit_file.read_bytes(), b"")
```

2. Add to `BrokerSessionTest` (tags G4, G5, K1):

```python
    def test_create_validates_an_empty_private_audit_file(self) -> None:
        self.assertTrue(self.session.audit_file.is_file())
        self.assertEqual(self.session.audit_file.read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(self.session.audit_file.stat().st_mode), 0o600)

    def test_deactivate_revokes_capability_without_removing_artifacts(self) -> None:
        request = self.request()
        self.session.deactivate()
        self.assertEqual(self.session._capability, "")
        self.assertTrue(self.session.capability_path.exists())
        with self.assertRaisesRegex(ValueError, "closed"):
            self.session.authorize(request)
        with self.assertRaisesRegex(ValueError, "closed"):
            self.session.audit(operation="pr-view", status="ok")
        self.session.close()
        self.assertFalse(self.session.run_dir.exists())

    def test_close_refuses_replaced_capability_and_is_retryable(self) -> None:
        run_dir = self.session.run_dir
        self.session.capability_path.unlink()
        self.session.capability_path.mkdir()
        with self.assertRaisesRegex(ValueError, "broker cleanup failed"):
            self.session.close()
        self.assertTrue(self.session.capability_path.is_dir())
        self.assertTrue(run_dir.exists())
        self.assertEqual(self.session._capability, "")
        self.session.capability_path.rmdir()
        self.session.close()
        self.assertFalse(run_dir.exists())
        self.session.close()

    def test_close_keeps_a_replaced_socket_inode_even_when_it_is_a_socket(self) -> None:
        run_dir = self.session.run_dir
        listener = self.session.open_listener()
        # Keep the original inode alive while the replacement is bound: ext4
        # reuses a freed inode number immediately.
        self.session.socket_path.unlink()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        replacement.bind(str(self.session.socket_path))
        replacement.close()
        listener.close()

        with self.assertRaisesRegex(ValueError, "broker cleanup failed"):
            self.session.close()
        self.assertTrue(stat.S_ISSOCK(self.session.socket_path.lstat().st_mode))
        self.assertFalse(self.session.capability_path.exists())
        self.assertTrue(run_dir.exists())

        self.session.socket_path.unlink()
        self.session.close()
        self.assertFalse(run_dir.exists())

    def test_audit_statuses_are_the_kernel_envelope_set(self) -> None:
        for status in ("client-disconnected", "timeout"):
            with self.subTest(status=status), self.assertRaisesRegex(
                ValueError, "broker audit status is invalid"
            ):
                self.session.audit(operation="pr-view", status=status)
        self.assertEqual(self.session.audit_file.read_bytes(), b"")
```

`socket` and `stat` are already imported in this test module (verify; add if not).

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_github_broker -v 2>&1 | tail -12`
Expected: the five new tests fail (`deactivate` missing, audit file absent, "changed during cleanup" instead of "broker cleanup failed", statuses accepted); the G5 edit fails because the file does not exist yet.

- [ ] **Step 3: Rewrite the session**

Replace the imports and helpers at the top of `src/agent_container/github_broker.py`:

```python
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import socket
import threading
from typing import Any

from agent_container.broker.artifacts import RuntimeArtifacts
from agent_container.broker.audit import AuditLog
from agent_container.broker.runtime import allocate_run_dir
from agent_container.broker.runtime import bind_private_listener
from agent_container.broker.runtime import create_private_file
from agent_container.broker.runtime import generate_capability
from agent_container.github_broker_error import BROKER_FAILURE_STAGES
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_broker_policy import validate_issue_number
from agent_container.github_broker_policy import validate_pr_number
from agent_container.github_broker_protocol import BrokerRequest
from agent_container.github_broker_protocol import PROTOCOL_VERSION
from agent_container.github_broker_protocol import MAX_REQUEST_NONCE
from agent_container.state import ensure_private_directory
from agent_container.state import github_broker_project_label


_LABEL = "broker"
_AUDIT_LABEL = "broker audit"
_AUDIT_STATUSES = frozenset({"ok", "denied", "error"})
_POLICY_VERSION = 1
```

Delete `_CAPABILITY`, `_MAX_UNIX_SOCKET_PATH_BYTES`, `_create_private_file`, `_open_audit_file`, and the `re`, `stat`, `TextIO`, `append_text_record` imports.

Dataclass fields:

```python
@dataclass
class BrokerSession:
    policy: BrokerPolicy
    run_id: str
    run_dir: Path
    socket_path: Path
    capability_path: Path
    audit_file: Path
    _capability: str = field(repr=False)
    _artifacts: RuntimeArtifacts = field(repr=False)
    _seen_sequences: set[int] = field(default_factory=set, repr=False)
    _listener: socket.socket | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)
    _cleanup_complete: bool = field(default=False, repr=False)
    _lifecycle_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
```

`create`:

```python
    @classmethod
    def create(cls, state_root: Path, policy: BrokerPolicy) -> "BrokerSession":
        root = ensure_private_directory(state_root)
        broker_root = ensure_private_directory(root / "github-broker", create=True)
        audit_root = ensure_private_directory(broker_root / "audit", create=True)
        run_root = ensure_private_directory(broker_root / "r", create=True)
        project_label = github_broker_project_label(policy.project_id)
        project_root = ensure_private_directory(
            run_root / project_label, create=True
        )
        audit_file = audit_root / "events.jsonl"
        AuditLog(audit_file, label=_AUDIT_LABEL).validate()

        run_id, run_dir = allocate_run_dir(project_root, label=_LABEL)
        try:
            capability = generate_capability(label=_LABEL)
        except RuntimeError:
            shutil.rmtree(run_dir)
            raise
        capability_path = run_dir / "capability"
        try:
            create_private_file(capability_path, capability + "\n", label=_LABEL)
        except Exception:
            shutil.rmtree(run_dir)
            raise
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
            policy=policy,
            run_id=run_id,
            run_dir=run_dir,
            socket_path=run_dir / "broker.sock",
            capability_path=capability_path,
            audit_file=audit_file,
            _capability=capability,
            _artifacts=artifacts,
        )
```

`authorize` keeps its body but reads `_closed` and `_capability` under the lock:

```python
    def authorize(self, request: BrokerRequest) -> dict[str, Any]:
        with self._lifecycle_lock:
            if self._closed:
                raise ValueError("broker session is closed")
            if request.version != PROTOCOL_VERSION:
                raise ValueError("broker protocol version is not supported")
            if not secrets.compare_digest(request.capability, self._capability):
                raise ValueError("broker request is not authorized")
            if request.project_id != self.policy.project_id:
                raise ValueError("broker request project is not allowed")
            if (
                not 1 <= request.sequence <= MAX_REQUEST_NONCE
                or request.sequence in self._seen_sequences
                or len(self._seen_sequences) >= 4096
            ):
                raise ValueError("broker request sequence is invalid")
            operation = self.policy.validate_operation(request.operation)
            self._seen_sequences.add(request.sequence)
        return {"operation": operation, "payload": request.payload}

    def deactivate(self) -> None:
        with self._lifecycle_lock:
            self._closed = True
            self._capability = ""

    def open_listener(self, backlog: int = 4) -> socket.socket:
        if self._closed or self._listener is not None:
            raise ValueError("broker listener state is invalid")
        listener = bind_private_listener(
            self.socket_path, backlog=backlog, label=_LABEL
        )
        try:
            self._artifacts.track_socket("broker.sock")
        except Exception:
            listener.close()
            raise
        self._listener = listener
        return listener
```

In `audit`, replace the final two lines (`with _open_audit_file(...) as stream: append_text_record(stream, record)`) with:

```python
        AuditLog(self.audit_file, label=_AUDIT_LABEL).append(record)
```

`close`:

```python
    def close(self) -> None:
        if self._cleanup_complete:
            return
        self.deactivate()
        cleanup_failed = False
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                cleanup_failed = True
            else:
                self._listener = None
        if self._artifacts.remove():
            cleanup_failed = True
        if cleanup_failed:
            raise ValueError("broker cleanup failed")
        self._cleanup_complete = True
```

- [ ] **Step 4: Run session tests, goldens, socket integration**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_github_broker tests.container.test_broker_github_golden tests.container.test_github_broker_transport tests.container.test_github_broker_runtime -v 2>&1 | tail -5`
Then: `AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.integration.test_github_broker_socket -v 2>&1 | tail -5`
Expected: all PASS. The pre-existing mocked-socket tests (`test_opens_private_unix_socket_and_cleans_runtime`, `test_listener_rejects_existing_path_and_double_open`, `test_rejects_overlong_socket_path_before_socket_creation`) pass unchanged because the patches act on the shared `socket`/`os` module objects and the kernel messages match. If any of them fails, stop: the label or patch seam assumption is wrong.

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/github_broker.py tests/container/test_github_broker.py
git commit -m "refactor: run the GitHub broker session on the kernel artifacts and audit log (G2-G5)"
```

---

### Task 2: `UploadPackBrokerRuntime` on `SocketBrokerRuntime` (G1, G8)

**Files:**
- Modify: `src/agent_container/github_broker_runtime.py`
- Modify: `tests/container/test_github_broker_compatibility.py` (delete the three runtime tests; Task 4 rewrites the protocol tests)
- Modify: `tests/container/test_github_broker_runtime.py`

**Interfaces:**
- Consumes: `SocketBrokerRuntime`, `Connection`, `SameUser`, `BrokerSession.deactivate/close/open_listener` (Task 1).
- Produces: `UploadPackBrokerRuntime` with the same public surface (`create`, `__enter__` → `BrokerRuntimeMount`, `__exit__`) plus `_runtime: SocketBrokerRuntime`; no `_stop`/`_thread`/`_error`/`_serve`.

- [ ] **Step 1: Write the failing tests**

Delete `test_start_preserves_original_error_and_closes_session`, `test_stop_order_and_cleanup_error_are_preserved`, and `test_serve_suppresses_errors_after_stop` from `tests/container/test_github_broker_compatibility.py` and remove its `UploadPackBrokerRuntime` import (tag G1).

Add to `tests/container/test_github_broker_runtime.py` (imports: `os`, `socket`, `struct`, `tempfile`, `threading`, `unittest`, `unittest.mock`, `BrokerPolicy`, `BrokerSession`, `UploadPackBrokerRuntime`, `GitHubBrokerRuntimeError`, `BrokerRuntimeMount` from `agent_container.podman`, `encode_request_frame`, `BrokerRequest`, `read_response_frame` from `agent_container.github_broker_protocol`):

```python
class UploadPackBrokerRuntimeLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ghb-runtime-")
        self.root = Path(self.temporary.name) / "state"
        self.root.mkdir(mode=0o700)
        self.policy = BrokerPolicy.create(
            project_id="agent-container",
            repository="jj1xgo/agent-container",
            default_branch="main",
            protected_branches=("main",),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _session(self) -> BrokerSession:
        return BrokerSession.create(self.root, self.policy)

    def test_enter_returns_mount_and_exit_removes_runtime(self) -> None:
        session = self._session()
        runtime = UploadPackBrokerRuntime(session, mock.Mock())
        mount = runtime.__enter__()
        try:
            self.assertEqual(mount, BrokerRuntimeMount(session.run_dir, self.policy.repository))
            self.assertTrue(session.socket_path.exists())
            self.assertEqual(runtime._runtime.thread.name, "github-broker")
        finally:
            runtime.__exit__(None, None, None)
        self.assertFalse(session.run_dir.exists())
        self.assertTrue(runtime._runtime.exited)

    def test_start_failure_is_reported_with_fixed_message_and_closes_session(self) -> None:
        session = self._session()
        runtime = UploadPackBrokerRuntime(session, mock.Mock())
        with mock.patch.object(
            session, "open_listener", side_effect=OSError("private-start-marker")
        ):
            with self.assertRaises(GitHubBrokerRuntimeError) as raised:
                runtime.__enter__()
        self.assertEqual(str(raised.exception), "GitHub broker failed to start")
        self.assertNotIn("private-start-marker", str(raised.exception))
        self.assertFalse(session.run_dir.exists())

    def test_deactivate_failure_still_removes_runtime_and_reports_fixed_error(self) -> None:
        session = self._session()
        calls = {"deactivate": 0}
        real_deactivate = session.deactivate

        def flaky_deactivate() -> None:
            calls["deactivate"] += 1
            if calls["deactivate"] == 1:
                raise ValueError("private-deactivate-marker")
            real_deactivate()

        run_dir = session.run_dir
        runtime = UploadPackBrokerRuntime(session, mock.Mock())
        with mock.patch.object(session, "deactivate", flaky_deactivate):
            runtime.__enter__()
            with self.assertRaises(GitHubBrokerRuntimeError) as raised:
                runtime.__exit__(None, None, None)
            self.assertEqual(str(raised.exception), "GitHub broker deactivate failed")
            self.assertFalse(run_dir.exists())
            self.assertFalse(runtime._runtime.exited)
            runtime.__exit__(None, None, None)
        self.assertTrue(runtime._runtime.exited)
        self.assertEqual(session._capability, "")

    def test_handler_failure_after_stop_is_reported(self) -> None:
        session = self._session()
        runtime = UploadPackBrokerRuntime(session, mock.Mock())
        entered = threading.Event()

        def explode(*_: object, **__: object) -> int:
            entered.set()
            raise RuntimeError("private-handler-marker")

        with mock.patch(
            "agent_container.github_broker_runtime.handle_broker_connection", explode
        ):
            runtime.__enter__()
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                client.connect(str(session.socket_path))
                self.assertTrue(entered.wait(2))
            finally:
                client.close()
            with self.assertRaises(GitHubBrokerRuntimeError) as raised:
                runtime.__exit__(None, None, None)
        self.assertEqual(str(raised.exception), "GitHub broker failed")
        self.assertNotIn("private-handler-marker", str(raised.exception))
        self.assertFalse(session.run_dir.exists())

    def test_peer_from_another_user_gets_no_bytes(self) -> None:
        session = self._session()
        runtime = UploadPackBrokerRuntime(session, mock.Mock())
        handled = threading.Event()

        def record(*_: object, **__: object) -> int:
            handled.set()
            return 0

        real_open = socket.socket
        foreign = struct.pack("3i", 4321, os.getuid() + 1, 0)

        with mock.patch(
            "agent_container.github_broker_runtime.handle_broker_connection", record
        ), mock.patch(
            "agent_container.broker.runtime.socket.socket.getsockopt",
            return_value=foreign,
        ):
            runtime.__enter__()
            client = real_open(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                client.settimeout(2)
                client.connect(str(session.socket_path))
                client.sendall(encode_request_frame(BrokerRequest(1, "A" * 43, "agent-container", 1, "issue-list", {})))
                self.assertEqual(client.recv(1), b"")
            finally:
                client.close()
            runtime.__exit__(None, None, None)
        self.assertFalse(handled.is_set())
```

If patching `socket.socket.getsockopt` on the class proves impossible (C type), replace that test with one that patches `agent_container.broker.runtime.open_connection` to return a `Connection` whose `peer_uid` is `os.getuid() + 1` and asserts the handler is never called and the client reads EOF.

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_github_broker_runtime.UploadPackBrokerRuntimeLifecycleTest -v 2>&1 | tail -8`
Expected: `AttributeError: _runtime`, raw `OSError` from start, `ValueError` from deactivate escaping.

- [ ] **Step 3: Rewrite the runtime**

In `src/agent_container/github_broker_runtime.py` replace the imports `accept_clients`, `socket` (if no longer used elsewhere in the module — check `_validate_policy_parent_identity` and the policy helpers before deleting `os`/`stat`/`socket` imports) with:

```python
from agent_container.broker.peer import SameUser
from agent_container.broker.runtime import Connection
from agent_container.broker.runtime import SocketBrokerRuntime
```

and add the constants next to `GitHubBrokerRuntimeError`:

```python
_LISTENER_TIMEOUT_SECONDS = 0.2
_CLIENT_TIMEOUT_SECONDS = 30
_STOP_TIMEOUT_SECONDS = 2
_LISTENER_BACKLOG = 4
```

Replace the `UploadPackBrokerRuntime` dataclass body (keep `create` verbatim):

```python
@dataclass
class UploadPackBrokerRuntime(AbstractContextManager[BrokerRuntimeMount]):
    session: BrokerSession
    transport: GitHubUploadPackTransport
    receive_transport: GitHubReceivePackTransport | None = None
    pr_transport: GitHubPullRequestTransport | None = None
    issue_transport: GitHubIssueTransport | None = None
    _runtime: SocketBrokerRuntime = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._runtime = SocketBrokerRuntime(
            label="GitHub broker",
            thread_name="github-broker",
            open_listener=lambda backlog: self.session.open_listener(backlog=backlog),
            handler=self._handle,
            deactivate=lambda: self.session.deactivate(),
            close=lambda: self.session.close(),
            error_type=GitHubBrokerRuntimeError,
            peer_policy=SameUser(),
            backlog=_LISTENER_BACKLOG,
            listener_timeout=_LISTENER_TIMEOUT_SECONDS,
            client_timeout=_CLIENT_TIMEOUT_SECONDS,
        )

    @classmethod
    def create(cls, layout: StateLayout, record: ProjectRecord) -> "UploadPackBrokerRuntime":
        ...  # unchanged

    def _handle(self, connection: Connection) -> int:
        return handle_broker_connection(
            self.session,
            connection.stream,
            self.transport,
            self.receive_transport,
            self.pr_transport,
            self.issue_transport,
        )

    def __enter__(self) -> BrokerRuntimeMount:
        self._runtime.start()
        return BrokerRuntimeMount(self.session.run_dir, self.session.policy.repository)

    def __exit__(self, *_: object) -> None:
        self._runtime.stop(join_timeout=_STOP_TIMEOUT_SECONDS)
```

`handle_broker_connection` is looked up as a module attribute at call time (tests patch `agent_container.github_broker_runtime.handle_broker_connection`); keep the plain `from ... import handle_broker_connection` and the call through the module name is not required because `mock.patch` on the module attribute rewrites the name the method resolves — verify with the handler test.

- [ ] **Step 4: Run runtime, agentctl, socket integration**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_github_broker_runtime tests.container.test_github_broker_compatibility tests.container.test_agentctl -v 2>&1 | tail -5`
Then the GitHub socket integration command from Global Constraints.
Expected: all PASS; `ResourceWarning` clean.

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/github_broker_runtime.py tests/container/test_github_broker_runtime.py tests/container/test_github_broker_compatibility.py
git commit -m "refactor: run the GitHub broker runtime on SocketBrokerRuntime with SameUser (G1, G8)"
```

---

### Task 3: kernel chunk writer through `write_all` (G6, kernel part)

**Files:**
- Modify: `src/agent_container/broker/frame.py`
- Modify: `tests/container/test_broker_frame.py`

**Interfaces:**
- Produces: `write_chunk_stream` writes header, chunk, and trailer with `write_all` (retrying short writes; `StreamError` on a zero write); `transferred` return unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/container/test_broker_frame.py`:

```python
class ChunkWriterTest(unittest.TestCase):
    def test_short_writes_are_retried_until_the_frame_is_complete(self) -> None:
        class ShortWriter(BytesIO):
            def write(self, body: bytes) -> int:
                return super().write(body[:1])

        stream = ShortWriter()
        transferred = write_chunk_stream(
            stream, (b"ab",), maximum_chunk=16, label="test stream"
        )
        self.assertEqual(transferred, 2)
        self.assertEqual(stream.getvalue(), bytes.fromhex("00000002616200000000"))

    def test_zero_write_is_a_stream_error(self) -> None:
        class Stuck(BytesIO):
            def write(self, body: bytes) -> int:
                return 0

        with self.assertRaises(StreamError) as raised:
            write_chunk_stream(Stuck(), (b"ab",), maximum_chunk=16, label="test stream")
        self.assertEqual(str(raised.exception), "test stream write failed")
```

Import `write_chunk_stream` in the test module if missing.

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_broker_frame.ChunkWriterTest -v 2>&1 | tail -6`
Expected: hex `006100` observed; no `StreamError` for the zero write.

- [ ] **Step 3: Implement**

In `src/agent_container/broker/frame.py`:

```python
def write_chunk_stream(
    stream: BinaryIO, chunks: Iterable[bytes], *, maximum_chunk: int, label: str
) -> int:
    transferred = 0
    for chunk in chunks:
        if not isinstance(chunk, bytes) or not chunk or len(chunk) > maximum_chunk:
            raise FrameSizeError(f"{label} chunk is invalid")
        _write_fully(stream, struct.pack(">I", len(chunk)), label=label)
        _write_fully(stream, chunk, label=label)
        transferred += len(chunk)
    _write_fully(stream, b"\x00\x00\x00\x00", label=label)
    stream.flush()
    return transferred


def _write_fully(stream: BinaryIO, frame: bytes, *, label: str) -> None:
    offset = 0
    while offset < len(frame):
        written = stream.write(frame[offset:])
        if (
            isinstance(written, bool)
            or not isinstance(written, int)
            or written <= 0
            or written > len(frame) - offset
        ):
            raise StreamError(f"{label} write failed")
        offset += written
```

and make `write_all` call `_write_fully(stream, frame, label=label)` followed by `stream.flush()` so the loop exists once. Keep one `flush()` per chunk stream (the Family compatibility tests count flushes on their own writer, not this one; confirm `tests/container/test_broker_github_primitives.py::test_chunk_writer_keeps_framing_and_rejects_empty_chunk` still holds — it asserts the final bytes, not write call counts).

- [ ] **Step 4: Run frame and primitives tests**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_broker_frame tests.container.test_broker_github_primitives tests.container.test_broker_frame_golden tests.container.test_broker_github_golden -v 2>&1 | tail -5`
Expected: all PASS (`test_short_write_behavior_and_clean_eof_are_preserved` in the compatibility module now fails; Task 4 rewrites it).

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/broker/frame.py tests/container/test_broker_frame.py
git commit -m "fix: retry short writes in the kernel chunk writer (G6)"
```

---

### Task 4: GitHub protocol on the kernel codec (G6)

**Files:**
- Modify: `src/agent_container/github_broker_protocol.py`
- Rewrite: `tests/container/test_github_broker_compatibility.py`

**Interfaces:**
- Produces: `encode_request_frame` via `encode_frame` (`ValueError("broker request is invalid")` for unserializable payloads, `"broker request is too large"` unchanged); `decode_response_frame` via `decode_frame` on `_RESPONSE_SCHEMA` (messages `broker response frame is incomplete` / `frame size is invalid` / `JSON is invalid` / `schema is invalid`; `NaN`/`Infinity` and duplicate keys rejected as JSON; bool version and non-str status rejected as schema); `read_request_frame` / `read_response_frame` via kernel `read_exact` (`broker stream is invalid` for `OSError`, `broker stream is incomplete` for EOF); `iter_chunk_stream` reads via kernel `read_exact(initial_eof=)`; `_read_exact` and `_HEADER_BYTES` deleted.
- Golden bytes unchanged (`_RESPONSE_SCHEMA` uses `JsonOptions(ensure_ascii=True, allow_nan=True, sort_keys=True, separators=(",", ":"), encoding="ascii")`, which reproduces today's `json.dumps(..., separators=(",", ":"), sort_keys=True).encode("ascii")`).

- [ ] **Step 1: Rewrite the compatibility tests**

Replace the protocol tests in `tests/container/test_github_broker_compatibility.py` with (tag G6):

```python
import io
import json
import sys
import unittest
from unittest import mock

from agent_container import github_broker_protocol as protocol
from agent_container.broker.frame import FrameJsonError
from agent_container.broker.frame import FrameSchemaError
from agent_container.broker.frame import StreamError
from agent_container.github_broker_protocol import BrokerResponse


def frame(body):
    return len(body).to_bytes(4, "big") + body


class GitHubKernelCodecTest(unittest.TestCase):
    def test_response_decoding_uses_kernel_error_kinds(self):
        cases = (
            (b"x", "broker response frame is incomplete"),
            (b"\x00\x00\x00\x00", "broker response frame size is invalid"),
            (b"\x00\x00\x00\x02x", "broker response frame is incomplete"),
            (frame(b'{"version":1,"version":1,"status":"ok"}'), "broker response JSON is invalid"),
            (frame(b'{"version":Infinity,"status":"ok"}'), "broker response JSON is invalid"),
            (frame(b'{"version":true,"status":"ok"}'), "broker response schema is invalid"),
            (frame(b'{"version":1,"status":[]}'), "broker response schema is invalid"),
            (frame(b'{"version":2,"status":"ok"}'), "broker response schema is invalid"),
        )
        for raw, message in cases:
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "^" + message + "$"):
                protocol.decode_response_frame(raw)
        response, consumed = protocol.decode_response_frame(frame(b'{"status":"ok","version":1}') + b"tail")
        self.assertEqual(response, BrokerResponse(1, "ok"))
        self.assertEqual(consumed, 4 + len(b'{"status":"ok","version":1}'))

    def test_request_encoding_reports_invalid_payloads_as_value_errors(self):
        with self.assertRaises(FrameSchemaError) as raised:
            protocol.encode_request_frame(
                protocol.BrokerRequest(1, "A" * 43, "demo", 1, "issue-list", {"x": object()})
            )
        self.assertEqual(str(raised.exception), "broker request is invalid")

    def test_stream_failures_are_stream_errors(self):
        stream = mock.Mock()
        stream.read.side_effect = OSError("synthetic-stream-error")
        with self.assertRaises(StreamError) as raised:
            protocol.read_request_frame(stream)
        self.assertEqual(str(raised.exception), "broker stream is invalid")
        with self.assertRaisesRegex(StreamError, "^broker stream is incomplete$"):
            protocol.read_response_frame(io.BytesIO(b"\x00\x00\x00\x05ab"))

    def test_request_json_errors_keep_the_github_decoder(self):
        with self.assertRaisesRegex(ValueError, "^broker request JSON is invalid$"):
            protocol.decode_request_frame(frame(b'{"version":1,"version":1}'))
        old = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(640)
            body = b'{"n":' + b"1" * 641 + b"}"
            with self.assertRaises(ValueError) as reference:
                json.loads(body)
            with self.assertRaises(ValueError) as actual:
                protocol.decode_request_frame(frame(body))
            self.assertEqual(str(actual.exception), str(reference.exception))
        finally:
            sys.set_int_max_str_digits(old)

    def test_chunk_streams_retry_short_writes_and_honor_initial_eof(self):
        class ShortWriter(io.BytesIO):
            def write(self, body):
                return super().write(body[:1])

        stream = ShortWriter()
        self.assertEqual(protocol.write_chunk_stream(stream, (b"ab",)), 2)
        self.assertEqual(stream.getvalue(), bytes.fromhex("00000002616200000000"))
        self.assertEqual(
            list(protocol.iter_chunk_stream(io.BytesIO(), maximum_total=0, allow_initial_eof=True)),
            [],
        )
        with self.assertRaisesRegex(StreamError, "^broker stream is incomplete$"):
            list(protocol.iter_chunk_stream(io.BytesIO(b"\x00"), maximum_total=0, allow_initial_eof=True))
        with self.assertRaises(FrameJsonError):
            protocol.decode_response_frame(frame(b"{"))
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_github_broker_compatibility -v 2>&1 | tail -8`
Expected: response cases fail on old messages; `TypeError` instead of `FrameSchemaError`; raw `OSError` instead of `StreamError`.

- [ ] **Step 3: Move the codec onto the kernel**

In `src/agent_container/github_broker_protocol.py`:

- Imports: add `encode_frame`, `read_exact`, `FrameSchemaError`, `FrameSizeError` from `agent_container.broker.frame`; drop `struct` if unused after the rewrite.
- Add after `_REQUEST_SCHEMA`:

```python
MAX_RESPONSE_BYTES = 1024
_RESPONSE_SCHEMA = FrameSchema(
    label="broker response",
    stream_label="broker stream",
    fields=_RESPONSE_FIELDS,
    max_bytes=MAX_RESPONSE_BYTES,
    json=JsonOptions(
        ensure_ascii=True,
        allow_nan=True,
        sort_keys=True,
        separators=(",", ":"),
        encoding="ascii",
    ),
)
```

- `encode_request_frame`:

```python
def encode_request_frame(request: BrokerRequest) -> bytes:
    try:
        return encode_frame(
            _REQUEST_SCHEMA,
            {
                "version": request.version,
                "capability": request.capability,
                "project_id": request.project_id,
                "sequence": request.sequence,
                "operation": request.operation,
                "payload": request.payload,
            },
        )
    except FrameSizeError:
        raise ValueError("broker request is too large") from None
```

(`encode_frame` raises `FrameSizeError("broker request is too large")` already; the re-raise keeps the message identical while documenting the intent — drop the `try` if the message is identical and the class is acceptable.)

- `encode_response_frame`: keep the version/status check, then `return encode_frame(_RESPONSE_SCHEMA, {"version": response.version, "status": response.status})`.
- `decode_response_frame`:

```python
def decode_response_frame(data: bytes) -> tuple[BrokerResponse, int]:
    decoded, consumed = decode_frame(_RESPONSE_SCHEMA, data)
    version = decoded["version"]
    status = decoded["status"]
    if isinstance(version, bool) or version != PROTOCOL_VERSION:
        raise FrameSchemaError("broker response schema is invalid")
    if not isinstance(status, str) or status not in _RESPONSE_STATUSES:
        raise FrameSchemaError("broker response schema is invalid")
    return BrokerResponse(version=version, status=status), consumed
```

- Delete `_read_exact` and `_HEADER_BYTES`; use `HEADER_BYTES` from the kernel:

```python
def read_request_frame(stream: BinaryIO) -> BrokerRequest:
    header = read_exact(stream, HEADER_BYTES, label="broker stream")
    length = int.from_bytes(header, "big")
    if length == 0 or length > MAX_REQUEST_BYTES:
        raise FrameSizeError("broker request frame size is invalid")
    body = read_exact(stream, length, label="broker stream")
    request, consumed = decode_request_frame(header + body)
    if consumed != len(header) + len(body):
        raise ValueError("broker request frame is invalid")
    return request


def read_response_frame(stream: BinaryIO) -> BrokerResponse:
    header = read_exact(stream, HEADER_BYTES, label="broker stream")
    length = int.from_bytes(header, "big")
    if length == 0 or length > MAX_RESPONSE_BYTES:
        raise FrameSizeError("broker response frame size is invalid")
    body = read_exact(stream, length, label="broker stream")
    response, consumed = decode_response_frame(header + body)
    if consumed != len(header) + len(body):
        raise ValueError("broker response frame is invalid")
    return response
```

- `iter_chunk_stream`: `read_bytes=lambda size, initial: read_exact(stream, size, label="broker stream", initial_eof=initial)`.
- Keep `_decode_request_json` and `decode_request_frame` as they are (typed validation and the GitHub JSON decoder stay).

- [ ] **Step 4: Run protocol, transport, golden, socket**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_github_broker_compatibility tests.container.test_github_broker_protocol tests.container.test_github_broker_transport tests.container.test_broker_github_golden tests.container.test_broker_github_primitives tests.container.test_github_client tests.container.test_git_remote_helper_cli -v 2>&1 | tail -5`
Then the GitHub socket integration command.
Expected: all PASS; golden bytes unchanged (`git diff --stat -- tests/fixtures` empty). If any test outside the compatibility module fails on a message string, tag it G6 in the PR body or stop if it is not a message-only change.

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/github_broker_protocol.py tests/container/test_github_broker_compatibility.py
git commit -m "refactor: run the GitHub broker codec on the kernel frame functions (G6)"
```

---

### Task 5: container-side readers on the kernel (G7)

**Files:**
- Modify: `src/agent_container/github_broker_transport.py` (only `_validate_exact_path`, `read_broker_capability`, `validate_broker_socket`, the `_CAPABILITY` regex, and now-unused imports)
- Modify: `tests/container/test_github_broker_transport.py` (additions only)

**Interfaces:**
- Produces: `read_broker_capability(path) -> str` = `read_capability(path, label="broker capability file")`; `validate_broker_socket(path) -> Path` = `validate_socket(validate_exact_path(path, label="broker runtime path"), label="broker socket")`; `_validate_exact_path(path)` = `validate_exact_path(path, label="broker runtime path")`. Names stay module-level (patch seams at L272-273, L310-311, L344-345).

- [ ] **Step 1: Write the failing tests**

Add to `BrokerRuntimePathTest` in `tests/container/test_github_broker_transport.py` (tag G7):

```python
    def test_rejects_capability_files_that_are_not_exactly_44_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for body in ("c" * 43, "c" * 43 + "\n\n", "c" * 42 + "\n"):
                path = Path(directory) / "capability"
                path.write_text(body, encoding="ascii")
                path.chmod(0o600)
                with self.subTest(body=body), self.assertRaisesRegex(ValueError, "broker capability file is invalid"):
                    read_broker_capability(path)
                path.unlink()

    def test_rejects_capability_replaced_after_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capability"
            path.write_text("c" * 43 + "\n", encoding="ascii")
            path.chmod(0o600)
            real_resolve = Path.resolve

            def swap_then_resolve(self_path: Path, *args: object, **kwargs: object) -> Path:
                resolved = real_resolve(self_path, *args, **kwargs)
                if resolved == path and path.read_bytes() == ("c" * 43 + "\n").encode():
                    path.unlink()
                    path.write_text("d" * 43 + "\n", encoding="ascii")
                    path.chmod(0o600)
                return resolved

            with mock.patch.object(Path, "resolve", swap_then_resolve):
                with self.assertRaisesRegex(ValueError, "broker capability file is invalid"):
                    read_broker_capability(path)
```

If the second test cannot be made deterministic against the kernel's exact call order, replace it with a test that a FIFO at the path is rejected (the kernel opens with `O_NONBLOCK` and rejects non-regular files):

```python
    def test_rejects_a_fifo_capability_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capability"
            os.mkfifo(path, 0o600)
            with self.assertRaisesRegex(ValueError, "broker capability file is invalid"):
                read_broker_capability(path)
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_github_broker_transport.BrokerRuntimePathTest -v 2>&1 | tail -6`
Expected: the 43-byte body without newline is currently accepted (size ≤ 45 and regex match after `removesuffix`) → FAIL; the FIFO test would block or error differently → FAIL.

- [ ] **Step 3: Replace the readers**

In `src/agent_container/github_broker_transport.py`:

```python
from agent_container.broker.capability import read_capability
from agent_container.broker.capability import validate_exact_path
from agent_container.broker.capability import validate_socket


def _validate_exact_path(path: Path) -> Path:
    return validate_exact_path(path, label="broker runtime path")


def read_broker_capability(path: Path) -> str:
    return read_capability(path, label="broker capability file")


def validate_broker_socket(path: Path) -> Path:
    return validate_socket(_validate_exact_path(path), label="broker socket")
```

Delete `_CAPABILITY` and the `re`, `stat`, `os` imports if nothing else in the module uses them (`grep -n "re\.\|stat\.\|os\." src/agent_container/github_broker_transport.py` first; `secrets` stays if used).

- [ ] **Step 4: Run transport, client, CLI, socket tests**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_github_broker_transport tests.container.test_github_client tests.container.test_git_remote_helper_cli -v 2>&1 | tail -5`
Then the GitHub socket integration command and `bin/lint`.
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/github_broker_transport.py tests/container/test_github_broker_transport.py
git commit -m "refactor: read GitHub broker capabilities through the kernel reader (G7)"
```

---

### Task 6: CHANGELOG, full verification, PR evidence

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add the Unreleased entry**

First bullet under `## [Unreleased]` → `### Added`:

```markdown
- Phase 6 stage 2のS2-2として、GitHub brokerを共通broker kernelへ完全に乗せ替えました。sessionは`RuntimeArtifacts`（identity付きcleanup、再試行可能なclose）、`AuditLog`（`create`時に空のaudit fileを検証作成、共通envelope）、kernelのcapability生成・private file・listener bindを使い、`deactivate()`を分離しました。runtimeは`SocketBrokerRuntime`（`SameUser` peer policy、30秒client timeout、fail-closed stop）の上で動き、起動・停止の失敗は`GitHub broker ...`の固定文で報告します。protocolはkernel codecに移り、response decodeは種別ごとのmessage、`NaN`／`Infinity`・bool version・非文字列statusを拒否、stream失敗は`broker stream is invalid`、chunk writerはshort writeを再試行します。container側のcapability readerはkernelの`read_capability`（size 44完全一致、open後のidentity再検証、`O_NONBLOCK`）になりました。wire byte、audit行、golden fixtureは不変です。意図的な変更は設計文書のG1〜G8です。
```

- [ ] **Step 2: Lint, suites, goldens**

```bash
bin/lint
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests/codex 2>&1 | tail -3
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests/container 2>&1 | tail -3
AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.integration.test_github_broker_socket tests.integration.test_handover_broker_socket tests.integration.test_egress_broker_socket 2>&1 | tail -3
git diff --check <base>...HEAD
git diff --stat <base>...HEAD -- tests/fixtures tests/container/test_broker_frame_golden.py tests/container/test_broker_audit_golden.py tests/container/test_broker_egress_golden.py tests/container/test_broker_github_golden.py tests/container/test_broker_family_golden.py
```

Expected: lint clean; codex 49 OK; container OK apart from the two sandbox-only failures; socket 8 OK without `ResourceWarning`; the golden `diff --stat` prints nothing. If the container total changed, update the fixed count in `docs/family-issue-create-broker-smoke-test.md`, `tests/container/test_docs.py`, and the stage 1 spec's count-exception paragraph in a separate commit (the S2-1 PR did the same for 1152).

- [ ] **Step 3: Intentional-change table for the PR body**

| file | tag |
| --- | --- |
| `tests/container/test_github_broker.py` | G5 (audit file exists empty), G4 (replaced inode, retry), G5/K5 (`_AUDIT_STATUSES` narrowed) |
| `tests/container/test_github_broker_compatibility.py` | G1 (runtime tests removed → `test_github_broker_runtime.py`), G6 (codec contracts) |
| `tests/container/test_github_broker_runtime.py` | G1 (lifecycle tests added) |
| `tests/container/test_broker_frame.py` | G6 (chunk writer) |
| `tests/container/test_github_broker_transport.py` | G7 (added reader tests) |

Any other edited existing test is untagged: stop and report.

- [ ] **Step 4: Commit and report**

```bash
git add CHANGELOG.md
git commit -m "docs: record stage 2 S2-2 GitHub broker unification in the changelog"
```

Report the commit range, verification outputs, `not run` items (local Podman, CI until the PR exists, real-host GitHub smoke — S2-4 reruns the existing guides), and the table above.

---

## Self-review

- **Spec coverage.** G1 → Task 2; G2/G3/G4/G5 → Task 1; G6 → Tasks 3, 4; G7 → Task 5; G8 (K1/K5 riding along) → Tasks 1, 2 tests; PR conditions → Task 6. `_AUDIT_STATUSES` narrowing is not a spec row: it removes two values production never emits and that K5 would now reject at append time; it is tagged G5/K5 and must be called out in the PR body as a ruling.
- **Placeholders.** Task 2 Step 3 marks `create` as unchanged (`...`) deliberately — the implementer keeps the existing body verbatim. Everything else carries the code.
- **Type consistency.** `RuntimeArtifacts` / `AuditLog` / `SocketBrokerRuntime` names and keyword arguments match S2-1's kernel; `Connection.stream` is what `handle_broker_connection` receives; `HEADER_BYTES` is the kernel constant; `MAX_RESPONSE_BYTES = 1024` matches today's literal.
