# Phase 6 stage 2 S2-3: egress E1〜E4 and Family F1〜F2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the stage 2 unification for the two brokers S2-1 left partially on the kernel: the egress container-side adapter reads its capability and socket through the kernel readers and connects with a bounded timeout (E1〜E4), and the Family intake runtime admits peers through the kernel's per-connection policy and cleans its run directory through `RuntimeArtifacts` (F1〜F2).

**Architecture:** egress: the host writes the capability with the kernel default mode `0600` (E1) so `egress_adapter.load_adapter_config` can become three one-line wrappers over `broker/capability.py` (E2); `open_gateway_tunnel` connects through `connect_unix(timeout=30)` and returns the socket to blocking mode before the relay (E3); the two literal `1` protocol versions become `PROTOCOL_VERSION` (E4). Family: `handle_family_intake_connection` takes a kernel `Connection`, the `SO_PEERCRED` read and the peer check move into `admit_connection` with a `FamilyPeerPolicy` that wraps `session.validate_peer` (F1); the two run-directory descriptors and the identity-checked socket/directory removal move into `RuntimeArtifacts`, which gains two read-only descriptor properties for the dir_fd-relative `chmod`/`stat` Family keeps doing itself (F2). Family's accept loop, self-stop after consumption, client interruption, `check()`, `FamilyRuntimeMount`, audit transaction, and `podman.py` do not change. egress's `_handle_client` (`raw_client=True`, tunnel reservation, audit stages) does not change.

**Tech Stack:** Python 3 standard library, `unittest`, `ruff` via `bin/lint`.

**Spec:** `docs/superpowers/specs/2026-09-06-broker-kernel-stage2-design.md` (K2, K3, 「### egress」E1〜E4, 「### Family」F1〜F2, 変えないもの, PR分割と受け入れ条件, 検証). Facts: `docs/superpowers/plans/2026-09-06-broker-kernel-stage2-investigation.md` §2 (Family lifecycle and cleanup), §4 (the three capability readers), §7 (CI and the two Family-only integration suites).

## Global Constraints

- Base: `main` at `b26b092` (PR #118, S2-2 merged) or later. Branch `feat/broker-kernel-stage2-3`. Kernel interfaces consumed: `read_capability(path, *, label)` (regular file, mode `0600`, owned by the current uid, size exactly 44 bytes, `O_NOFOLLOW|O_NONBLOCK`, post-open `resolve`/`lstat` identity re-check, failure `ValueError("<label> is invalid")`); `validate_exact_path(path, *, label)` (absolute and `resolve(strict=True)` equal); `validate_socket(path, *, label)` (`S_ISSOCK`, mode `0600`, current uid; `path.stat()` errors propagate as `OSError`); `connect_unix(path, *, timeout, socket_factory)` (`settimeout` then `connect`, closes on any failure and re-raises); `Connection(client, stream, peer_uid, peer_pid, peer_gid)`; `open_connection(client, *, timeout)`; `admit_connection(client, *, timeout, policy) -> Connection | None` (`None` = denied, stream already closed, nothing read or written; a policy exception closes the stream and propagates); `PeerPolicy.admit(connection) -> bool`; `RuntimeArtifacts.open(run_dir, *, label)` / `track_socket(name)` / `remove() -> bool` (True = failed; descriptors kept for a retry) / `close()`; `PROTOCOL_VERSION` from `egress_broker_protocol`.
- Wire bytes unchanged: `tests/container/test_broker_egress_golden.py` request/response/audit bytes and `tests/fixtures/broker_family_golden.json` pass **without edits** except the single E1 mode assertion (`test_broker_egress_golden.py` L133, `0o400` → `0o600`), which the spec lists as the only intentional golden edit.
- Audit bytes unchanged: egress `EgressBrokerSession.audit` and Family `append_family_audit` are not touched.
- Fixed messages that must survive: egress adapter `"egress adapter configuration is invalid"` (environment parsing), `"egress gateway request failed"` (tunnel), `"egress CONNECT request is invalid"`; Family `"family intake runtime failed to start"`, `"family intake runtime did not stop"`, `"family intake runtime cleanup failed"`, `"family intake runtime failed"`, `"family intake cleanup failed"` (inner cleanup), `"family intake run directory is not private"` (kernel label `"family intake"` reproduces it), `"family intake persistence failed"`.
- New messages introduced by E2 (spec: 失敗messageは `<label> is invalid`): `"egress adapter capability is invalid"`, `"egress adapter socket is invalid"`.
- Intentional behavior changes are exactly the rows E1〜E4 and F1〜F2 of the spec. Every edited existing test is listed in the PR body with its item number. `test_family_kernel_runtime.py` and the fake `_GatewaySocket` in `test_egress_adapter.py` are edited because the `_serve` admission path (F1) and the `connect_unix` call (E3) now touch `getsockopt`/`makefile`/`settimeout` on the fakes; both are tagged in Task 3 / Task 6. An edit that cannot be tagged means behavior changed unintentionally: stop and report.
- Not touched: `family_intake_broker.py`, `family_intake_client.py`, `family_intake_protocol.py`, `family_runtime_mount.py`, `family_pending.py`, `podman.py`, `egress_broker_runtime._handle_client`, `egress_gateway.py`, `egress_policy.py`, `egress_broker_protocol.py`, all handover and GitHub sources, `StateLayout`/`FamilyStateLayout`, the Mount types.
- Family's `FamilyIntakeRuntime` keeps `_serve`, `_client`, `_client_lock`, `_stop`, `_error`, `_listener`, `_thread`, `_mount`, `_cleanup_complete`, `session`, `is_alive()`, `check()`, `register_runtime()`, `validate_mount()` with their current names (`test_family_kernel_runtime.py` and `tests/integration/test_family_intake_socket.py` reach them).
- `family_intake_runtime.py` keeps `import socket`, `import os`, `import threading`, `import stat` (tests patch `agent_container.family_intake_runtime.socket.socket`, `...os.open`, `...os.chmod`; those patch the shared module objects, so they keep intercepting the kernel's `os.open` too).
- Local commands: `bin/lint`; `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest <module>`; full container suite `... discover -s tests/container`; CI socket integration `AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.integration.test_github_broker_socket tests.integration.test_handover_broker_socket tests.integration.test_egress_broker_socket`; Family-only (not in CI, required by the spec for any PR touching Family): `AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.integration.test_family_intake_socket` (10 tests) and `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.integration.test_family_forced_unknown` (4 tests). Run the container suite once with `TMPDIR` on an ext4 directory outside the repo tree as well as on tmpfs (inode-reuse regressions only show on ext4).
- Sandbox notes: `.git/worktrees` and `.github` are read-only; work on a branch checked out in `/workspace`; never stage `.github/workflows/ci.yml`; in the sandbox `test_agent_handover_wrapper...test_create_uses_fixed_environment_scope_and_session` fails for environment reasons and `test_docs`' CI-file test may fail when the working-tree `ci.yml` is stale (CI is the arbiter).

---

## File structure

| File | Responsibility after S2-3 |
| --- | --- |
| `src/agent_container/egress_broker.py` (modify) | capability file created with the kernel default mode (E1); `_CAPABILITY_FILE_MODE` and its comment removed |
| `src/agent_container/egress_adapter.py` (modify) | `load_adapter_config` validates the socket path with `validate_exact_path`+`validate_socket` and reads the capability with `validate_exact_path`+`read_capability` (E2); `open_gateway_tunnel` connects via `connect_unix(timeout=30)` then `settimeout(None)` (E3); `PROTOCOL_VERSION` (E4); private reader, `stat`, `_CAPABILITY`, `_NOFOLLOW` removed |
| `src/agent_container/egress_broker_runtime.py` (modify) | `_write_response` uses `PROTOCOL_VERSION` (E4) |
| `src/agent_container/broker/artifacts.py` (modify) | `RuntimeArtifacts.dir_fd` / `parent_dir_fd` read-only properties (K2 addition for F2) |
| `src/agent_container/family_intake_transport.py` (modify) | `FamilyPeerPolicy` (wraps `session.validate_peer`, `FamilyIntakeDenied` → `False`); `handle_family_intake_connection(connection: Connection, session, store)` without credential reading (F1) |
| `src/agent_container/family_intake_runtime.py` (modify) | `_serve` admits through `admit_connection(..., policy=FamilyPeerPolicy(session))` (F1); run directory and socket cleanup through `RuntimeArtifacts` (F2); `_run_*` descriptor fields, `_socket_stat`, `_private_directory`, `_DIRECTORY`, `_CLOEXEC`, `_NOFOLLOW` removed |
| `tests/container/test_egress_broker.py` (modify, E1) | capability mode assertion `0o600` |
| `tests/container/test_broker_egress_golden.py` (modify, E1) | capability mode assertion `0o600` with an E1 comment |
| `tests/container/test_egress_adapter.py` (modify, E2/E3/E4) | config tests on the kernel readers; fake gateway records `settimeout`; connect-failure test |
| `tests/container/test_broker_artifacts.py` (modify, additions only) | descriptor property test |
| `tests/container/test_family_intake_runtime.py` (modify, F1/F2) | descriptor assertions through `_artifacts`; unregistered-peer test |
| `tests/container/test_family_intake_transport.py` (rewrite entry, F1) | handler tests build `Connection`; peer denial tests move to `FamilyPeerPolicy` + `admit_connection` |
| `tests/container/test_family_kernel_runtime.py` (modify, F1) | fake client answers `getsockopt`/`makefile`; fake session has `validate_peer`; denied-peer loop test |
| `CHANGELOG.md`, `docs/family-issue-create-broker-smoke-test.md`, `tests/container/test_docs.py`, `docs/superpowers/specs/2026-09-04-broker-kernel-design.md` (modify) | Unreleased entry; fixed container suite count |

---

### Task 1: egress host capability mode `0600` (E1)

**Files:**
- Modify: `src/agent_container/egress_broker.py:30-34,101-107`
- Modify: `tests/container/test_egress_broker.py:43-46`
- Modify: `tests/container/test_broker_egress_golden.py:132-134`

**Interfaces:**
- Consumes: `create_private_file(path, body, *, label, mode=0o600)` (kernel default).
- Produces: the run directory's `capability` file has mode `0600`; Task 2's container-side reader depends on it.

- [ ] **Step 1: Update the two existing assertions (tag E1)**

In `tests/container/test_egress_broker.py`, `test_creates_private_project_scoped_runtime`, change

```python
        self.assertEqual(
            stat.S_IMODE(self.session.capability_path.stat().st_mode), 0o400
        )
```

to

```python
        self.assertEqual(
            stat.S_IMODE(self.session.capability_path.stat().st_mode), 0o600
        )
```

In `tests/container/test_broker_egress_golden.py`, inside `test_session_audit_lines_match_golden_bytes` (the block ending at the `finally: session.close()`), change

```python
                self.assertEqual(
                    stat.S_IMODE(session.capability_path.stat().st_mode), 0o400
                )
```

to

```python
                # Stage 2 E1: the host writes the capability with the kernel
                # default mode 0600 so the container-side kernel reader accepts
                # it. Golden request/response/audit bytes above are unchanged.
                self.assertEqual(
                    stat.S_IMODE(session.capability_path.stat().st_mode), 0o600
                )
```

- [ ] **Step 2: Run the two modules to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_egress_broker tests.container.test_broker_egress_golden 2>&1 | tail -5`
Expected: FAIL, both with `AssertionError: 256 != 384` (`0o400` observed, `0o600` expected).

- [ ] **Step 3: Write the capability with the kernel default mode**

In `src/agent_container/egress_broker.py` delete lines 32-34:

```python
# The container-side adapter reads the capability through a read-only bind
# mount and rejects writable files, so the file is created owner-read-only.
_CAPABILITY_FILE_MODE = 0o400
```

and change the `create_private_file` call in `create` to

```python
        try:
            create_private_file(capability_path, capability + "\n", label=_LABEL)
        except Exception:
            shutil.rmtree(run_dir)
            raise
```

- [ ] **Step 4: Run the modules to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_egress_broker tests.container.test_broker_egress_golden tests.container.test_egress_broker_runtime 2>&1 | tail -3`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/egress_broker.py tests/container/test_egress_broker.py tests/container/test_broker_egress_golden.py
git commit -m "refactor: write the egress capability with the kernel default mode (E1)"
```

---

### Task 2: egress adapter configuration on the kernel readers (E2, E4 adapter half)

**Files:**
- Modify: `src/agent_container/egress_adapter.py:1-32,106-147,150-167`
- Modify: `tests/container/test_egress_adapter.py:1-25,125-171`

**Interfaces:**
- Consumes: `read_capability`, `validate_exact_path`, `validate_socket` from `agent_container.broker.capability`; `PROTOCOL_VERSION` from `agent_container.egress_broker_protocol`.
- Produces: `load_adapter_config(environment) -> AdapterConfig` that requires an existing private (`0600`, current uid) Unix socket at `AGENT_EGRESS_SOCKET` and a private 44-byte capability at `AGENT_EGRESS_CAPABILITY`, raising `ValueError("egress adapter socket is invalid")` / `ValueError("egress adapter capability is invalid")`; `_CAPABILITY_LABEL = "egress adapter capability"`, `_SOCKET_LABEL = "egress adapter socket"`. `open_gateway_tunnel` sends `EgressRequest(PROTOCOL_VERSION, ...)`.

Ruling recorded here (spec E2 says only "socket検証を `validate_socket` に置き換え"): the socket is validated **once, when the configuration is loaded**, in the same place the capability is read. Validating per CONNECT inside `open_gateway_tunnel` would make the existing tunnel tests (fake socket paths such as `/run/broker.sock`) fail, and E3 permits additions only there. In the container the broker socket exists before the adapter starts (`EgressBrokerRuntime.__enter__` binds the listener before `podman run`).

- [ ] **Step 1: Write the failing tests (tag E2)**

In `tests/container/test_egress_adapter.py` add to the imports:

```python
from agent_container.egress_broker_protocol import PROTOCOL_VERSION
```

Add these helpers directly above `class AdapterGatewayTest`:

```python
def _write_capability(path: Path, body: str, mode: int) -> None:
    path.write_text(body, encoding="ascii")
    path.chmod(mode)


def _bind_private_socket(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    os.chmod(path, 0o600)
    return listener


def _environment(root: Path) -> dict[str, str]:
    return {
        "AGENT_EGRESS_SOCKET": str(root / "broker.sock"),
        "AGENT_EGRESS_CAPABILITY": str(root / "capability"),
        "AGENT_PROJECT_ID": "demo-project",
        "AGENT_EGRESS_AGENT": "codex",
    }
```

Replace `test_loads_exact_fixed_environment_and_read_only_capability` and `test_rejects_writable_symlink_or_malformed_capability` (lines 125-171) with:

```python
    def test_loads_exact_fixed_environment_private_capability_and_socket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _write_capability(root / "capability", "A" * 43 + "\n", 0o600)
            listener = _bind_private_socket(root / "broker.sock")
            try:
                config = load_adapter_config(_environment(root))
            finally:
                listener.close()

            self.assertEqual(config.socket_path, root / "broker.sock")
            self.assertEqual(config.project_id, "demo-project")
            self.assertEqual(config.agent, "codex")
            self.assertNotIn("A" * 43, repr(config))

    def test_rejects_non_private_malformed_or_symlinked_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            listener = _bind_private_socket(root / "broker.sock")
            try:
                capability_path = root / "capability"
                cases = (
                    ("A" * 43 + "\n", 0o400),
                    ("A" * 43 + "\n", 0o444),
                    ("A" * 43 + "\n", 0o644),
                    ("bad value\n", 0o600),
                    ("A" * 42 + "\n", 0o600),
                    ("A" * 43, 0o600),
                    ("A" * 43 + "\n\n", 0o600),
                )
                for body, mode in cases:
                    capability_path.unlink(missing_ok=True)
                    _write_capability(capability_path, body, mode)
                    with self.subTest(body=body, mode=oct(mode)), self.assertRaisesRegex(
                        ValueError, "^egress adapter capability is invalid$"
                    ):
                        load_adapter_config(_environment(root))
                target = root / "target"
                _write_capability(target, "A" * 43 + "\n", 0o600)
                capability_path.unlink()
                capability_path.symlink_to(target)
                with self.assertRaisesRegex(
                    ValueError, "^egress adapter capability is invalid$"
                ):
                    load_adapter_config(_environment(root))
                capability_path.unlink()
                with self.assertRaisesRegex(
                    ValueError, "^egress adapter capability is invalid$"
                ):
                    load_adapter_config(_environment(root))
            finally:
                listener.close()

    def test_rejects_missing_non_socket_shared_or_relative_socket_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _write_capability(root / "capability", "A" * 43 + "\n", 0o600)
            socket_path = root / "broker.sock"

            with self.subTest(case="missing"), self.assertRaisesRegex(
                ValueError, "^egress adapter socket is invalid$"
            ):
                load_adapter_config(_environment(root))

            socket_path.write_text("", encoding="ascii")
            socket_path.chmod(0o600)
            with self.subTest(case="regular file"), self.assertRaisesRegex(
                ValueError, "^egress adapter socket is invalid$"
            ):
                load_adapter_config(_environment(root))
            socket_path.unlink()

            listener = _bind_private_socket(socket_path)
            try:
                os.chmod(socket_path, 0o666)
                with self.subTest(case="shared mode"), self.assertRaisesRegex(
                    ValueError, "^egress adapter socket is invalid$"
                ):
                    load_adapter_config(_environment(root))
                os.chmod(socket_path, 0o600)
                relative = _environment(root) | {"AGENT_EGRESS_SOCKET": "broker.sock"}
                with self.subTest(case="relative"), self.assertRaisesRegex(
                    ValueError, "^egress adapter socket is invalid$"
                ):
                    load_adapter_config(relative)
                missing_key = dict(_environment(root))
                del missing_key["AGENT_EGRESS_AGENT"]
                with self.subTest(case="environment"), self.assertRaisesRegex(
                    ValueError, "^egress adapter configuration is invalid$"
                ):
                    load_adapter_config(missing_key)
            finally:
                listener.close()
```

`os`, `socket`, `tempfile`, `Path` are already imported in this module.

- [ ] **Step 2: Run the module to verify the new tests fail**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_egress_adapter 2>&1 | tail -15`
Expected: `test_loads_exact_fixed_environment_private_capability_and_socket` FAIL (`0o600` rejected by the old reader: `egress adapter configuration is invalid`), `test_rejects_non_private_malformed_or_symlinked_capability` FAIL (the `0o400`/`0o444` cases load successfully, and the message regex does not match), `test_rejects_missing_non_socket_shared_or_relative_socket_path` FAIL (missing socket loads successfully). Everything else OK.

- [ ] **Step 3: Replace the adapter's private reader with the kernel readers**

In `src/agent_container/egress_adapter.py`:

1. Imports: delete `import stat`; add after `from typing import Sequence`:

```python
from agent_container.broker.capability import read_capability
from agent_container.broker.capability import validate_exact_path
from agent_container.broker.capability import validate_socket
```

and add to the protocol imports:

```python
from agent_container.egress_broker_protocol import PROTOCOL_VERSION
```

2. Delete the module constants `_CAPABILITY = re.compile(r"[A-Za-z0-9_-]{43}")` and `_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)`; add

```python
_CAPABILITY_LABEL = "egress adapter capability"
_SOCKET_LABEL = "egress adapter socket"
```

3. Replace `_read_capability` (lines 106-133) and `load_adapter_config` (lines 136-147) with:

```python
def _read_adapter_capability(path: Path) -> str:
    return read_capability(
        validate_exact_path(path, label=_CAPABILITY_LABEL), label=_CAPABILITY_LABEL
    )


def _validate_adapter_socket(path: Path) -> Path:
    try:
        return validate_socket(
            validate_exact_path(path, label=_SOCKET_LABEL), label=_SOCKET_LABEL
        )
    except OSError:
        raise ValueError(f"{_SOCKET_LABEL} is invalid") from None


def load_adapter_config(environment: Mapping[str, str]) -> AdapterConfig:
    try:
        socket_path = Path(environment["AGENT_EGRESS_SOCKET"])
        capability_path = Path(environment["AGENT_EGRESS_CAPABILITY"])
        project_id = validate_project_id(environment["AGENT_PROJECT_ID"])
        agent = validate_agent(environment["AGENT_EGRESS_AGENT"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("egress adapter configuration is invalid") from None
    socket_path = _validate_adapter_socket(socket_path)
    capability = _read_adapter_capability(capability_path)
    return AdapterConfig(socket_path, capability, project_id, agent)
```

4. In `open_gateway_tunnel` change `EgressRequest(1, ...)` to `EgressRequest(PROTOCOL_VERSION, ...)` (E4; the body is rewritten again in Task 3, keep the constant there).

`re` stays imported (`_REQUEST_LINE`, `_HEADER_NAME`); `os` stays (`os.write`, `os.close`, `os.environ`).

- [ ] **Step 4: Run the module and lint**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_egress_adapter 2>&1 | tail -3 && bin/lint`
Expected: OK; lint clean (no unused `stat`/`_CAPABILITY`/`_NOFOLLOW`).

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/egress_adapter.py tests/container/test_egress_adapter.py
git commit -m "refactor: read the egress adapter capability and socket through the kernel readers (E2)"
```

---

### Task 3: bounded gateway connect and `PROTOCOL_VERSION` (E3, E4 runtime half)

**Files:**
- Modify: `src/agent_container/egress_adapter.py` (`open_gateway_tunnel`)
- Modify: `src/agent_container/egress_broker_runtime.py:13-15,144-148`
- Modify: `tests/container/test_egress_adapter.py` (`_GatewaySocket`, `test_opens_authenticated_tunnel_and_returns_connected_socket`, one new test)

**Interfaces:**
- Consumes: `connect_unix(path, *, timeout, socket_factory)`.
- Produces: `_GATEWAY_CONNECT_TIMEOUT_SECONDS = 30`; `open_gateway_tunnel` returns a blocking socket (`gettimeout() is None`) and raises the fixed `ValueError("egress gateway request failed")` for connect timeouts and refusals.

- [ ] **Step 1: Write the failing tests (tag E3; the fake gains `settimeout`)**

In `tests/container/test_egress_adapter.py`, extend `_GatewaySocket`:

```python
class _GatewaySocket:
    def __init__(self, response: bytes) -> None:
        self.response = io.BytesIO(response)
        self.sent = bytearray()
        self.connected_to: object = None
        self.closed = False
        self.timeouts: list[object] = []

    def settimeout(self, timeout: object) -> None:
        self.timeouts.append(timeout)

    def connect(self, address: object) -> None:
        self.connected_to = address

    def sendall(self, body: bytes) -> None:
        self.sent.extend(body)

    def makefile(self, _mode: str) -> io.BytesIO:
        return self.response

    def close(self) -> None:
        self.closed = True


class _RefusingGatewaySocket(_GatewaySocket):
    def connect(self, address: object) -> None:
        raise TimeoutError("private-connect-marker")
```

In `test_opens_authenticated_tunnel_and_returns_connected_socket` add after `self.assertEqual(gateway.connected_to, str(config.socket_path))`:

```python
        self.assertEqual(gateway.timeouts, [30, None])
```

and after the `(request.project_id, ...)` assertion:

```python
        self.assertEqual(request.version, PROTOCOL_VERSION)
```

Add a new test after `test_denial_closes_tunnel_and_uses_fixed_error`:

```python
    def test_connect_failure_closes_gateway_and_uses_fixed_error(self) -> None:
        gateway = _RefusingGatewaySocket(b"")
        config = AdapterConfig(Path("/run/broker.sock"), "A" * 43, "demo", "codex")

        with self.assertRaisesRegex(ValueError, "^egress gateway request failed$") as raised:
            open_gateway_tunnel(
                config,
                "api.example.com",
                1,
                socket_factory=lambda *_args: gateway,
            )

        self.assertTrue(gateway.closed)
        self.assertEqual(gateway.timeouts, [30])
        self.assertEqual(bytes(gateway.sent), b"")
        self.assertNotIn("private-connect-marker", str(raised.exception))
```

- [ ] **Step 2: Run the module to verify the new assertions fail**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_egress_adapter 2>&1 | tail -12`
Expected: `test_opens_authenticated_tunnel...` FAIL (`[] != [30, None]`); `test_connect_failure_closes_gateway_and_uses_fixed_error` FAIL (`[] != [30]` — the old code connects without `settimeout`, the refusal is still converted to the fixed error).

- [ ] **Step 3: Connect through `connect_unix`**

In `src/agent_container/egress_adapter.py` add to the kernel imports:

```python
from agent_container.broker.capability import connect_unix
```

add the constant next to `MAX_CONNECT_HEADER_BYTES`:

```python
_GATEWAY_CONNECT_TIMEOUT_SECONDS = 30
```

and replace `open_gateway_tunnel` with:

```python
def open_gateway_tunnel(
    config: AdapterConfig,
    domain: str,
    sequence: int,
    socket_factory: Callable[..., socket.socket] = socket.socket,
) -> socket.socket:
    try:
        gateway = connect_unix(
            config.socket_path,
            timeout=_GATEWAY_CONNECT_TIMEOUT_SECONDS,
            socket_factory=socket_factory,
        )
    except (OSError, ValueError):
        raise ValueError("egress gateway request failed") from None
    try:
        # E3: only the connect wait is bounded. The request/response exchange
        # and the relay stay blocking, as before the kernel connect helper.
        gateway.settimeout(None)
        request = EgressRequest(
            PROTOCOL_VERSION,
            config.capability,
            config.project_id,
            sequence,
            "connect",
            domain,
            443,
        )
        gateway.sendall(encode_request_frame(request))
        stream = gateway.makefile("rb")
        try:
            response = read_response_frame(stream)
        finally:
            stream.close()
        if response.status != "ok" or response.code != "connect":
            raise ValueError("egress gateway request failed")
        return gateway
    except (OSError, ValueError):
        gateway.close()
        raise ValueError("egress gateway request failed") from None
```

- [ ] **Step 4: `PROTOCOL_VERSION` in the runtime response (E4)**

In `src/agent_container/egress_broker_runtime.py` add to the protocol imports:

```python
from agent_container.egress_broker_protocol import PROTOCOL_VERSION
```

and change `_write_response` to:

```python
    def _write_response(
        self, stream: BinaryIO, status: str, code: str
    ) -> None:
        stream.write(encode_response_frame(EgressResponse(PROTOCOL_VERSION, status, code)))
        stream.flush()
```

- [ ] **Step 5: Run the egress modules, the egress socket integration, and lint**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_egress_adapter tests.container.test_egress_broker_runtime tests.container.test_egress_broker_runtime_surface tests.container.test_broker_egress_golden 2>&1 | tail -3 && AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.integration.test_egress_broker_socket 2>&1 | tail -3 && bin/lint`
Expected: OK, OK, lint clean. The socket test's final `open_gateway_tunnel` against the removed run directory still raises `ValueError` (the `FileNotFoundError` from `connect` is an `OSError`).

- [ ] **Step 6: Commit**

```bash
git add src/agent_container/egress_adapter.py src/agent_container/egress_broker_runtime.py tests/container/test_egress_adapter.py
git commit -m "refactor: bound the egress gateway connect and use PROTOCOL_VERSION (E3, E4)"
```

---

### Task 4: `RuntimeArtifacts` descriptor properties (K2 addition for F2)

**Files:**
- Modify: `src/agent_container/broker/artifacts.py:67-70`
- Modify: `tests/container/test_broker_artifacts.py` (additions only)

**Interfaces:**
- Produces: `RuntimeArtifacts.dir_fd -> int` (the run directory descriptor) and `RuntimeArtifacts.parent_dir_fd -> int` (the parent descriptor), both raising `ValueError("<label> run directory is closed")` after `close()` or a successful `remove()`. Task 5 uses `dir_fd` for `stat`/`chmod` of `intake.sock`; the Family tests use both to prove the descriptors are closed after cleanup.

- [ ] **Step 1: Write the failing test**

Add to `RuntimeArtifactsTest` in `tests/container/test_broker_artifacts.py`:

```python
    def test_exposes_descriptors_while_open_and_refuses_them_after_close(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = _run_dir(Path(directory))
            artifacts = RuntimeArtifacts.open(run_dir, label=LABEL)
            try:
                self.assertEqual(
                    (
                        os.fstat(artifacts.dir_fd).st_ino,
                        os.fstat(artifacts.parent_dir_fd).st_ino,
                    ),
                    (run_dir.stat().st_ino, run_dir.parent.stat().st_ino),
                )
                probe = os.stat(".", dir_fd=artifacts.dir_fd)
                self.assertEqual(probe.st_ino, run_dir.stat().st_ino)
            finally:
                artifacts.close()
            for name in ("dir_fd", "parent_dir_fd"):
                with self.subTest(name=name), self.assertRaisesRegex(
                    ValueError, "^test broker run directory is closed$"
                ):
                    getattr(artifacts, name)
```

- [ ] **Step 2: Run the module to verify it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_broker_artifacts 2>&1 | tail -5`
Expected: ERROR `AttributeError: 'RuntimeArtifacts' object has no attribute 'dir_fd'`.

- [ ] **Step 3: Add the properties**

In `src/agent_container/broker/artifacts.py`, after `_require_open`:

```python
    @property
    def dir_fd(self) -> int:
        """Run directory descriptor for dir_fd-relative operations on tracked names."""
        self._require_open()
        return self._descriptor

    @property
    def parent_dir_fd(self) -> int:
        """Parent directory descriptor; the run directory is removed through it."""
        self._require_open()
        return self._parent_descriptor
```

- [ ] **Step 4: Run the module**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_broker_artifacts 2>&1 | tail -3`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add src/agent_container/broker/artifacts.py tests/container/test_broker_artifacts.py
git commit -m "feat: expose the RuntimeArtifacts directory descriptors (K2)"
```

---

### Task 5: Family run directory and socket cleanup on `RuntimeArtifacts` (F2)

**Files:**
- Modify: `src/agent_container/family_intake_runtime.py`
- Modify: `tests/container/test_family_intake_runtime.py:264-380`

**Interfaces:**
- Consumes: `RuntimeArtifacts.open(run_dir, *, label="family intake")`, `track_socket("intake.sock")`, `dir_fd`, `parent_dir_fd`, `remove()`, `close()`.
- Produces: `FamilyIntakeRuntime._artifacts: RuntimeArtifacts | None` (set by `_create_run_directory`, cleared by `_cleanup_artifacts`); `_LABEL = "family intake"`. `_run_descriptor`, `_run_parent_descriptor`, `_run_id`, `_run_stat`, `_socket_stat`, `_private_directory`, `_DIRECTORY`, `_CLOEXEC`, `_NOFOLLOW` no longer exist.

Behavior kept (spec F2 観測挙動 なし): a replaced socket or directory inode is preserved and reported with the fixed inner message `family intake cleanup failed` (which `close()` wraps as `family intake runtime cleanup failed`); `did not stop` is raised before cleanup; both descriptors are closed even when removal fails, and `_cleanup_complete` is set even on failure (Family's close is not retryable, unchanged). Difference in mechanism only: when the socket removal fails, the kernel still attempts `rmdir` and fails on the non-empty directory instead of skipping it; the outcome (directory kept, failure reported) is identical.

- [ ] **Step 1: Rewrite the descriptor assertions (tag F2)**

In `tests/container/test_family_intake_runtime.py`:

1. In `test_run_directory_open_failure_cleans_the_inode_created_by_this_start` and `test_post_bind_start_failure_cleans_the_owned_socket_and_run_directory`, replace

```python
        self.assertIsNone(runtime._run_descriptor)
        self.assertIsNone(runtime._run_parent_descriptor)
```

with

```python
        self.assertIsNone(runtime._artifacts)
```

2. In `test_startup_cleanup_preserves_replacement_and_closes_descriptors`, replace inside `replace_socket_then_fail`

```python
            run_descriptor = runtime._run_descriptor
            parent_descriptor = runtime._run_parent_descriptor
            self.assertIsNotNone(run_descriptor)
            self.assertIsNotNone(parent_descriptor)
            descriptors.extend((run_descriptor, parent_descriptor))  # type: ignore[arg-type]
```

with

```python
            artifacts = runtime._artifacts
            self.assertIsNotNone(artifacts)
            run_descriptor = artifacts.dir_fd  # type: ignore[union-attr]
            parent_descriptor = artifacts.parent_dir_fd  # type: ignore[union-attr]
            descriptors.extend((run_descriptor, parent_descriptor))
```

and at the end replace the two `assertIsNone(runtime._run_*)` lines with `self.assertIsNone(runtime._artifacts)`.

3. In `test_cleanup_preserves_replacement_inode_and_reports_fixed_failure`, replace

```python
        run_descriptor = runtime._run_descriptor
        parent_descriptor = runtime._run_parent_descriptor
        self.assertIsNotNone(run_descriptor)
        self.assertIsNotNone(parent_descriptor)
```

with

```python
        artifacts = runtime._artifacts
        self.assertIsNotNone(artifacts)
        run_descriptor = artifacts.dir_fd  # type: ignore[union-attr]
        parent_descriptor = artifacts.parent_dir_fd  # type: ignore[union-attr]
```

and the two `assertIsNone(runtime._run_*)` lines with `self.assertIsNone(runtime._artifacts)`.

- [ ] **Step 2: Run the module to verify the four tests fail**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_family_intake_runtime 2>&1 | grep -E '^(ERROR|FAIL|Ran|OK|FAILED)'`
Expected: the four tests ERROR with `AttributeError: 'FamilyIntakeRuntime' object has no attribute '_artifacts'`; the rest OK.

- [ ] **Step 3: Move the descriptors and the cleanup into `RuntimeArtifacts`**

In `src/agent_container/family_intake_runtime.py`:

1. Imports: add `from agent_container.broker.artifacts import RuntimeArtifacts` before the `accept_clients` import. Keep `import os`, `import socket`, `import stat`, `import threading`.

2. Constants: delete `_DIRECTORY`, `_NOFOLLOW`, `_CLOEXEC`; add `_LABEL = "family intake"`. Delete the helper `_private_directory` (keep `_same_inode`).

3. Fields: replace

```python
    _run_parent_descriptor: int | None = field(default=None, init=False, repr=False)
    _run_descriptor: int | None = field(default=None, init=False, repr=False)
    _run_id: str | None = field(default=None, init=False, repr=False)
    _run_stat: os.stat_result | None = field(default=None, init=False, repr=False)
    _socket_stat: os.stat_result | None = field(default=None, init=False, repr=False)
```

with

```python
    _artifacts: RuntimeArtifacts | None = field(default=None, init=False, repr=False)
```

4. Replace `_create_run_directory` with:

```python
    def _create_run_directory(self) -> Path:
        ensure_private_directory(self.layout.root)
        for directory in (
            self.layout.family_root,
            self.layout.family_root / "intake",
            self.layout.family_root / "intake" / "r",
            self.layout.family_intake_run_root,
        ):
            ensure_private_directory(directory, create=True)
        for _attempt in range(_RUN_ID_ATTEMPTS):
            generated = self.random_bytes(8)
            if type(generated) is not bytes or len(generated) != 8:
                raise ValueError("family intake random source is invalid")
            run_dir = self.layout.family_intake_run_root / generated.hex()
            try:
                run_dir.mkdir(mode=0o700)
            except FileExistsError:
                continue
            try:
                # Opens the parent and the run directory by descriptor, requires
                # mode 0700 and the current uid, and captures the identity that
                # cleanup compares against (K2).
                self._artifacts = RuntimeArtifacts.open(run_dir, label=_LABEL)
            except BaseException:
                try:
                    os.rmdir(run_dir)
                except OSError:
                    pass
                raise
            return run_dir
        raise FileExistsError("could not allocate family intake runtime")
```

5. In `start()`, replace the block from `listener.bind(str(socket_path))` through `raise PermissionError("family intake socket is not private")` with:

```python
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            artifacts = self._artifacts
            if artifacts is None:
                raise ValueError("family intake run directory is unavailable")
            # Capture the bound socket's identity before chmod: cleanup only
            # unlinks this inode (F2).
            artifacts.track_socket("intake.sock")
            dir_fd = artifacts.dir_fd
            socket_stat = os.stat("intake.sock", dir_fd=dir_fd, follow_symlinks=False)
            os.chmod("intake.sock", 0o600, dir_fd=dir_fd, follow_symlinks=False)
            secured = os.stat("intake.sock", dir_fd=dir_fd, follow_symlinks=False)
            if (
                not _same_inode(socket_stat, secured)
                or not stat.S_ISSOCK(secured.st_mode)
                or stat.S_IMODE(secured.st_mode) != 0o600
                or secured.st_uid != os.getuid()
            ):
                raise PermissionError("family intake socket is not private")
```

(`listener.listen(...)` and everything after it are unchanged.)

6. Replace `_cleanup_artifacts` with:

```python
    def _cleanup_artifacts(self) -> None:
        artifacts = self._artifacts
        self._artifacts = None
        self._cleanup_complete = True
        if artifacts is None:
            return
        try:
            cleanup_failed = artifacts.remove()
        finally:
            # Family closes both descriptors even when removal failed; its
            # close() is not retried (unchanged behavior).
            artifacts.close()
        if cleanup_failed:
            raise ValueError("family intake cleanup failed")
```

`close()`, `start()`'s failure path, `_serve`, `_fail_runtime`, `check()` are unchanged.

- [ ] **Step 4: Run the Family modules and lint**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_family_intake_runtime tests.container.test_family_kernel_runtime tests.container.test_broker_family_golden 2>&1 | tail -3 && bin/lint`
Expected: OK; lint clean (no unused constants or helpers).

- [ ] **Step 5: Run the Family socket integration on the real filesystem**

Run: `AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.integration.test_family_intake_socket 2>&1 | tail -3`
Expected: `Ran 10 tests`, OK, no `ResourceWarning` (the descriptors are closed by `_cleanup_artifacts`).

- [ ] **Step 6: Commit**

```bash
git add src/agent_container/family_intake_runtime.py tests/container/test_family_intake_runtime.py
git commit -m "refactor: clean the family intake run directory through RuntimeArtifacts (F2)"
```

---

### Task 6: Family peer admission through the kernel `Connection` (F1)

**Files:**
- Modify: `src/agent_container/family_intake_transport.py`
- Modify: `src/agent_container/family_intake_runtime.py` (`_serve`, imports)
- Modify: `tests/container/test_family_intake_transport.py`
- Modify: `tests/container/test_family_kernel_runtime.py`
- Modify: `tests/container/test_family_intake_runtime.py` (one new test)

**Interfaces:**
- Consumes: `Connection`, `admit_connection` from `agent_container.broker.runtime`; `FamilyIntakeSession.validate_peer(peer_pid, peer_uid)` (raises `FamilyIntakeDenied` for every denial; its `_read_process` converts reader errors to `FamilyIntakeDenied`).
- Produces: `FamilyPeerPolicy(session).admit(connection) -> bool`; `handle_family_intake_connection(connection: Connection, session: FamilyIntakeSession, store: Path) -> None` (no credential reading; closes `connection.stream` in `finally`). `_serve` calls `admit_connection(client, timeout=_CLIENT_TIMEOUT_SECONDS, policy=FamilyPeerPolicy(self.session))` inside `with client:` and hands an admitted `Connection` to the handler.

Behavior kept (spec F1 観測挙動 なし): a denied peer's connection is closed without reading a byte, without a response, without audit, without consuming the capability; a non-`FamilyIntakeDenied` exception from `validate_peer` still fails the runtime (closed, `family intake runtime failed`), now via `admit_connection` propagating it instead of the handler. Order change without observable effect: the peer check now precedes the `owns_store` check (both deny silently and read nothing).

- [ ] **Step 1: Rewrite the transport tests on `Connection` (tag F1)**

Replace `tests/container/test_family_intake_transport.py` lines 1-59 (imports, `FakeStream`, `FakeConnection`) with:

```python
from io import BytesIO
import os
from pathlib import Path
import socket
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_container.broker.runtime import admit_connection
from agent_container.broker.runtime import Connection
from agent_container.family_intake_broker import FamilyIntakeInternalError
from agent_container.family_intake_broker import FamilyIntakeSession
from agent_container.family_intake_protocol import decode_response_frame
from agent_container.family_intake_protocol import encode_request_frame
from agent_container.family_intake_protocol import FamilyIntakeRequest
from agent_container.family_intake_transport import FamilyPeerPolicy
from agent_container.family_intake_transport import handle_family_intake_connection
from agent_container.family_pending import list_pending


NOW = 1_800_000_000
PEER_PID = 4242
CAPABILITY = "c" * 43


class FakeStream:
    def __init__(self, incoming: bytes, *, fail_write: bool = False) -> None:
        self.incoming = BytesIO(incoming)
        self.outgoing = BytesIO()
        self.fail_write = fail_write
        self.closed = False

    def read(self, size: int) -> bytes:
        return self.incoming.read(size)

    def write(self, body: bytes) -> int:
        if self.fail_write:
            raise BrokenPipeError("private-disconnect-marker")
        return self.outgoing.write(body)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeClient:
    """Accepted socket as admit_connection sees it: settimeout, SO_PEERCRED, makefile."""

    def __init__(self, stream: FakeStream, *, pid: int = PEER_PID, uid: int = os.getuid()) -> None:
        self.stream = stream
        self.pid = pid
        self.uid = uid
        self.timeout: object = None
        self.credential_calls: list[tuple[int, int, int]] = []

    def settimeout(self, timeout: object) -> None:
        self.timeout = timeout

    def getsockopt(self, level: int, option: int, size: int) -> bytes:
        self.credential_calls.append((level, option, size))
        return struct.pack("3i", self.pid, self.uid, 9999)

    def makefile(self, *_: object, **__: object) -> FakeStream:
        return self.stream


def admitted(stream: FakeStream, *, pid: int = PEER_PID, uid: int = os.getuid()) -> Connection:
    return Connection(None, stream, uid, pid, 9999)
```

Then in the test class:

1. `test_reads_peer_credentials_and_writes_one_exact_response` becomes:

```python
    # Break caught: the handler writing more than one frame or leaving the stream open.
    def test_writes_one_exact_response_for_an_admitted_connection(self) -> None:
        session = self.session()
        stream = FakeStream(encode_request_frame(self.request()))

        handle_family_intake_connection(admitted(stream), session, self.store)

        response, consumed = decode_response_frame(stream.outgoing.getvalue())
        self.assertEqual(consumed, len(stream.outgoing.getvalue()))
        self.assertEqual(
            (response.version, response.status, response.request_id, response.expires_at),
            (1, "pending", "22" * 16, 1_800_086_400),
        )
        self.assertTrue(stream.closed)
```

2. In `test_disconnect_before_complete_frame_leaves_capability_reusable`, `test_disconnect_after_persistence_keeps_one_pending_and_consumes_run`, `test_internal_failure_is_silent_to_client_but_propagates_to_supervisor`, and `test_rejects_store_mismatch_without_consumption`, replace every `FakeConnection(<stream>)` with `admitted(<stream>)`.

3. Replace `test_peer_mismatch_is_silent_and_does_not_read_or_consume` with three tests:

```python
    # Break caught: a different process consuming the capability before the expected runtime.
    def test_policy_denies_unregistered_pid_before_a_byte_is_read(self) -> None:
        session = self.session()
        stream = FakeStream(encode_request_frame(self.request()))
        client = FakeClient(stream, pid=PEER_PID + 1)

        connection = admit_connection(client, timeout=30, policy=FamilyPeerPolicy(session))

        self.assertIsNone(connection)
        self.assertEqual(client.timeout, 30)
        self.assertEqual(
            client.credential_calls, [(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)]
        )
        self.assertEqual(stream.incoming.tell(), 0)
        self.assertEqual(stream.outgoing.getvalue(), b"")
        self.assertTrue(stream.closed)
        self.assertFalse(session.consumed)
        self.assertEqual(list_pending(self.store, "demo"), ())

    # Break caught: a same-pid connection from another uid being admitted.
    def test_policy_denies_foreign_uid_and_admits_the_registered_runtime(self) -> None:
        session = self.session()
        denied = FakeClient(FakeStream(b""), uid=os.getuid() + 1)
        self.assertIsNone(
            admit_connection(denied, timeout=30, policy=FamilyPeerPolicy(session))
        )
        self.assertTrue(denied.stream.closed)

        stream = FakeStream(encode_request_frame(self.request()))
        connection = admit_connection(
            FakeClient(stream), timeout=30, policy=FamilyPeerPolicy(session)
        )

        self.assertIsNotNone(connection)
        self.assertEqual(
            (connection.peer_pid, connection.peer_uid),  # type: ignore[union-attr]
            (PEER_PID, os.getuid()),
        )
        self.assertIs(connection.stream, stream)  # type: ignore[union-attr]
        self.assertFalse(stream.closed)
        self.assertEqual(stream.incoming.tell(), 0)

    # Break caught: an unexpected validation failure being downgraded to a silent denial.
    def test_policy_propagates_unexpected_validation_failures_and_closes_the_stream(self) -> None:
        session = self.session()
        stream = FakeStream(encode_request_frame(self.request()))
        with patch.object(
            session, "validate_peer", side_effect=RuntimeError("private-peer-marker")
        ):
            with self.assertRaises(RuntimeError):
                admit_connection(
                    FakeClient(stream), timeout=30, policy=FamilyPeerPolicy(session)
                )
        self.assertTrue(stream.closed)
        self.assertEqual(stream.incoming.tell(), 0)
        self.assertFalse(session.consumed)
```

- [ ] **Step 2: Update the kernel-boundary loop tests (tag F1)**

In `tests/container/test_family_kernel_runtime.py`:

1. Add imports `import os` and `import struct` at the top, and `from agent_container.family_intake_broker import FamilyIntakeDenied`.

2. Extend `Client`:

```python
class Stream:
    def __init__(self, events):
        self.events = events

    def close(self):
        self.events.append("stream-close")


class Client:
    def __init__(self, events, *, pid=4242):
        self.events = events
        self.closed = 0
        self.timeout = None
        self.pid = pid
        self.stream = Stream(events)

    def __enter__(self):
        self.events.append("enter")
        return self

    def __exit__(self, *_):
        self.events.append("exit")
        self.close()

    def close(self):
        self.closed += 1
        self.events.append("client-close")

    def settimeout(self, timeout):
        self.timeout = timeout
        self.events.append("timeout")

    def getsockopt(self, _level, _option, _size):
        self.events.append("peercred")
        return struct.pack("3i", self.pid, os.getuid(), 0)

    def makefile(self, *_args, **_kwargs):
        self.events.append("makefile")
        return self.stream

    def shutdown(self, _how):
        self.events.append("shutdown")
```

3. `make_runtime` gains a fake `validate_peer` that denies pid `1`:

```python
def make_runtime(listener, *, consumed=False, failed=False):
    runtime = FamilyIntakeRuntime(
        FamilyStateLayout(Path("/synthetic/state"), "demo"),
        agent="codex", repository="demo",
    )

    def validate_peer(peer_pid, peer_uid):
        if peer_pid == 1:
            raise FamilyIntakeDenied()

    runtime.session = SimpleNamespace(
        consumed=consumed, failed=failed, validate_peer=validate_peer
    )
    runtime._listener = listener
    return runtime
```

4. In `test_timeout_then_consumed_request_closes_client_before_listener`, the handler receives a `Connection`:

```python
        def handle(observed, session, store):
            self.assertIs(observed.client, client)
            self.assertIs(observed.stream, client.stream)
            self.assertEqual((observed.peer_pid, observed.peer_uid), (4242, os.getuid()))
            self.assertIs(runtime._client, client)
            self.assertIs(session, runtime.session)
            self.assertEqual(store, runtime.layout.family_pending_dir)
            events.append("handle")
```

and its expected sequence becomes

```python
        self.assertEqual(events, ["accept", "accept", "enter", "timeout", "peercred", "makefile", "handle", "exit", "client-close", "listener-close"])
```

5. In `test_handler_failure_and_failed_session_release_client_before_failure`, the expected sequence becomes

```python
                self.assertEqual(events, ["accept", "enter", "timeout", "peercred", "makefile", "handle", "exit", "client-close", "listener-close"])
```

6. Add a new test:

```python
    def test_denied_peer_is_closed_unread_and_the_loop_continues(self):
        events = []
        denied = Client(events, pid=1)
        stopping = Client(events)
        listener = Listener(events, ())
        runtime = make_runtime(listener)

        def stopping_accept():
            runtime._stop.set()
            return stopping

        listener.actions = iter((denied, stopping_accept))

        def handle(*_):
            raise AssertionError("denied peer reached the handler")

        with patch("agent_container.family_intake_runtime.handle_family_intake_connection", handle):
            runtime._serve(listener)
        self.assertEqual(events, [
            "accept", "enter", "timeout", "peercred", "makefile", "stream-close",
            "exit", "client-close", "accept", "client-close",
        ])
        self.assertEqual(denied.timeout, 30)
        self.assertIsNone(runtime._client)
        self.assertFalse(runtime._error)
        self.assertEqual(listener.closed, 0)
```

- [ ] **Step 3: Add the runtime-level denial test (tag F1, addition)**

In `tests/container/test_family_intake_runtime.py` add `from agent_container.family_pending import list_pending` to the imports and this test after `test_context_creates_bounded_private_socket_and_cleans_owned_artifacts`:

```python
    # Break caught: an unregistered process reading a frame or consuming the capability.
    def test_unregistered_peer_is_closed_without_response_or_consumption(self) -> None:
        runtime = self.runtime()
        with runtime as mount:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(2)
            try:
                client.connect(str(mount.socket_path))
                client.sendall(b"\x00\x00\x00\x01x")
                try:
                    self.assertEqual(client.recv(1), b"")
                except ConnectionResetError:
                    pass
            finally:
                client.close()
            self.assertFalse(runtime.session.consumed)
            self.assertEqual(list_pending(self.layout.family_pending_dir, "demo"), ())
            self.assertTrue(runtime.is_alive())
        self.assertFalse(mount.socket_dir.exists())
```

- [ ] **Step 4: Run the three modules to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_family_intake_transport tests.container.test_family_kernel_runtime tests.container.test_family_intake_runtime 2>&1 | grep -E '^(ERROR|FAIL|Ran|OK|FAILED)'`
Expected: transport module ERROR at import (`cannot import name 'FamilyPeerPolicy'`); kernel-runtime tests FAIL on the event sequences (no `peercred`/`makefile`) and the denied-peer test reaches the handler (`AssertionError`); `test_unregistered_peer_is_closed_without_response_or_consumption` passes already (the old transport also denies silently) — that is expected, it pins the behavior across the change.

- [ ] **Step 5: Implement `FamilyPeerPolicy` and the `Connection` handler**

Replace `src/agent_container/family_intake_transport.py` with:

```python
"""One-frame Unix connection handling for family intake."""

from pathlib import Path
from typing import BinaryIO

from agent_container.broker.runtime import Connection
from agent_container.family_intake_broker import FamilyIntakeSession
from agent_container.family_intake_broker import FamilyIntakeDenied
from agent_container.family_intake_broker import FamilyIntakeInternalError
from agent_container.family_intake_protocol import read_request_frame
from agent_container.family_intake_protocol import write_response_frame


class FamilyPeerPolicy:
    """Admit only the registered runtime's process tree.

    A denial closes the connection unread (kernel `admit_connection`): no
    response, no audit, no capability consumption. Any exception other than
    `FamilyIntakeDenied` propagates and fails the runtime closed.
    """

    def __init__(self, session: FamilyIntakeSession) -> None:
        self._session = session

    def admit(self, connection: Connection) -> bool:
        try:
            self._session.validate_peer(connection.peer_pid, connection.peer_uid)
        except FamilyIntakeDenied:
            return False
        return True


def handle_family_intake_connection(
    connection: Connection,
    session: FamilyIntakeSession,
    store: Path,
) -> None:
    """Close ordinary denials silently; propagate sanitized internal failures."""

    stream: BinaryIO = connection.stream
    try:
        if not session.owns_store(store):
            raise ValueError("family intake store is invalid")
        request = read_request_frame(stream)
        response = session.handle(request)
        write_response_frame(stream, response)
    except FamilyIntakeInternalError:
        raise
    except FamilyIntakeDenied:
        return
    except (OSError, TypeError, ValueError):
        return
    finally:
        try:
            stream.close()
        except (OSError, TypeError, ValueError):
            pass
```

- [ ] **Step 6: Admit through the kernel in `_serve`**

In `src/agent_container/family_intake_runtime.py` add the imports

```python
from agent_container.broker.runtime import admit_connection
from agent_container.family_intake_transport import FamilyPeerPolicy
```

and replace the inner `with client:` block of `_serve` with:

```python
                try:
                    with client:
                        if self.session is None:
                            raise ValueError("family intake session is unavailable")
                        # F1: settimeout, SO_PEERCRED and the peer check happen in
                        # the kernel; a denied peer is closed before any read.
                        connection = admit_connection(
                            client,
                            timeout=_CLIENT_TIMEOUT_SECONDS,
                            policy=FamilyPeerPolicy(self.session),
                        )
                        if connection is not None:
                            handle_family_intake_connection(
                                connection,
                                self.session,
                                self.layout.family_pending_dir,
                            )
                finally:
```

(the `finally` that clears `self._client`, the `session.failed` / `session.consumed` checks, and the `except BaseException` are unchanged).

- [ ] **Step 7: Run the Family modules, both Family integration suites, and lint**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_family_intake_transport tests.container.test_family_kernel_runtime tests.container.test_family_intake_runtime tests.container.test_family_intake_broker tests.container.test_broker_family_golden tests.container.test_family_kernel_compatibility 2>&1 | tail -3 && AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.integration.test_family_intake_socket 2>&1 | tail -3 && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.integration.test_family_forced_unknown 2>&1 | tail -3 && bin/lint`
Expected: OK; `Ran 10 tests` OK without `ResourceWarning` (`test_registered_runtime_root_execs_client_while_sibling_is_denied` exercises the real `SO_PEERCRED` path through the policy); `Ran 4 tests` OK; lint clean. `test_runtime_environment_and_intake_imports_contain_no_sensitive_surface` still passes: `agent_container.broker.runtime` is not a forbidden import.

- [ ] **Step 8: Commit**

```bash
git add src/agent_container/family_intake_transport.py src/agent_container/family_intake_runtime.py tests/container/test_family_intake_transport.py tests/container/test_family_kernel_runtime.py tests/container/test_family_intake_runtime.py
git commit -m "refactor: admit family intake peers through the kernel connection policy (F1)"
```

---

### Task 7: CHANGELOG, fixed suite count, full verification, PR evidence

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `docs/family-issue-create-broker-smoke-test.md:27`, `tests/container/test_docs.py:885`, `docs/superpowers/specs/2026-09-04-broker-kernel-design.md:145`

- [ ] **Step 1: Add the Unreleased entry**

Insert as the first bullet under `## [Unreleased]` → `### Added` in `CHANGELOG.md`:

```markdown
- Phase 6 stage 2のS2-3として、egressとFamilyの残りを共通broker kernelへ乗せ替えました。egressはhost側capability fileをkernel既定のmode `0600`で作り（E1）、container側adapterのcapability／socket検証をkernelの`read_capability`／`validate_exact_path`／`validate_socket`に置き換え（E2: `0400`／`0444`を拒否し`0600`のみ受理、実行user所有、size 44完全一致、`O_NONBLOCK`、失敗は`egress adapter capability is invalid`／`egress adapter socket is invalid`）、gatewayへの接続を`connect_unix`の30秒timeoutで区切って接続後にblockingへ戻し（E3）、literalの`1`を`PROTOCOL_VERSION`にしました（E4）。Family intakeは`SO_PEERCRED`の読み取りとpeer検証をkernelの`admit_connection`と`FamilyPeerPolicy`（`validate_peer`を包む）に移し、拒否した接続は従来どおり1 byteも読まず応答もauditも書かずに閉じます（F1）。run directoryとsocketの片付けは`RuntimeArtifacts`（identity検査付き、descriptor保持）に委譲し、差し替えinodeの温存と固定message、descriptorの確実なcloseは変わりません（F2）。`RuntimeArtifacts`に`dir_fd`／`parent_dir_fd`を追加しました。wire byte、audit行、golden fixtureは不変（E1のmode断言のみ更新）です。意図的な変更は設計文書のE1〜E4／F1〜F2です。
```

- [ ] **Step 2: Lint, suites, goldens**

```bash
bin/lint
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests/codex 2>&1 | tail -3
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests/container 2>&1 | tail -3
TMPDIR=<ext4 dir outside the repo> PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests/container 2>&1 | tail -3
AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.integration.test_github_broker_socket tests.integration.test_handover_broker_socket tests.integration.test_egress_broker_socket 2>&1 | tail -3
AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.integration.test_family_intake_socket 2>&1 | tail -3
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.integration.test_family_forced_unknown 2>&1 | tail -3
git diff --check <base>...HEAD
git diff --stat <base>...HEAD -- tests/fixtures tests/container/test_broker_frame_golden.py tests/container/test_broker_audit_golden.py tests/container/test_broker_egress_golden.py tests/container/test_broker_github_golden.py tests/container/test_broker_family_golden.py
```

Expected: lint clean; codex 49 OK; container OK apart from the sandbox-only failures; socket 8 OK, Family socket 10 OK, forced-unknown 4 OK, none with `ResourceWarning`; the golden `diff --stat` lists only `tests/container/test_broker_egress_golden.py` with the 4-line E1 change (3 comment lines + the mode). Record the container total. It changes (Task 2 +1 test, Task 3 +1, Task 4 +1, Task 6 transport −1 +3, kernel-runtime +1, runtime +1 → expected 1162 + 7 = 1169; use the observed number).

- [ ] **Step 3: Update the fixed container count in a separate commit**

Replace `1162` with the observed total in `docs/family-issue-create-broker-smoke-test.md` line 27 (`container suiteは\`Ran 1162 tests ... OK\``) and `tests/container/test_docs.py` line 885, and append to the count-exception paragraph of `docs/superpowers/specs/2026-09-04-broker-kernel-design.md` (line 145, after the S2-2 sentence):

```markdown
 stage 2のS2-3（egress E1〜E4とFamily F1〜F2、kernel unit test追加）を取り込んだ基準では、container期待値を<observed>とします。
```

Run `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.container.test_docs 2>&1 | tail -3` (the CI-file test may fail in the sandbox for the stale `ci.yml`; everything else OK), then:

```bash
git add docs/family-issue-create-broker-smoke-test.md tests/container/test_docs.py docs/superpowers/specs/2026-09-04-broker-kernel-design.md
git commit -m "docs: update the fixed container suite count for stage 2 S2-3"
```

- [ ] **Step 4: Intentional-change table for the PR body**

| file | tag |
| --- | --- |
| `tests/container/test_egress_broker.py` | E1 (capability mode `0600`) |
| `tests/container/test_broker_egress_golden.py` | E1 (mode assertion only; bytes untouched) |
| `tests/container/test_egress_adapter.py` | E2 (config tests on the kernel readers, socket validated at load), E3 (`_GatewaySocket.settimeout`, timeout assertions, connect-failure test), E4 (`request.version == PROTOCOL_VERSION`) |
| `tests/container/test_broker_artifacts.py` | K2 (descriptor properties, addition) |
| `tests/container/test_family_intake_runtime.py` | F2 (descriptor assertions via `_artifacts`), F1 (unregistered-peer test, addition) |
| `tests/container/test_family_intake_transport.py` | F1 (entry on `Connection`; peer denial tests on `FamilyPeerPolicy` + `admit_connection`) |
| `tests/container/test_family_kernel_runtime.py` | F1 (fake client answers `SO_PEERCRED`/`makefile`; sequences gain `peercred`/`makefile`; denied-peer loop test) |
| `tests/container/test_docs.py` | fixed container count |

Any other edited existing test is untagged: stop and report.

Rulings to state in the PR body: (1) the egress socket is validated once at configuration load (Task 2 note); (2) only the connect wait is bounded in E3, the handshake and relay stay blocking (spec: 接続後に `settimeout(None)`); (3) Family's cleanup still closes both descriptors and sets `_cleanup_complete` on failure (not retryable, unchanged), the kernel's retryable `remove()` is not exposed to Family in S2-3; (4) `RuntimeArtifacts.dir_fd`/`parent_dir_fd` are new kernel surface (K2) because Family keeps its own dir_fd-relative `chmod`/`stat`.

- [ ] **Step 5: Commit the CHANGELOG and report**

```bash
git add CHANGELOG.md
git commit -m "docs: record stage 2 S2-3 egress and Family unification in the changelog"
```

Report the commit range, verification outputs, `not run` items (local Podman: unavailable in the sandbox; the required CI Podman gate exercises the real adapter in the container and is the first place E2's `validate_exact_path`/uid check on `/run/agent-egress/*` is proven; real-host egress and Family smoke — S2-4 reruns the existing guides), and the table above. Stage 1 design sentences that still say the egress capability is `0400` (`docs/superpowers/specs/2026-09-04-broker-kernel-design.md` L36, L41) are historical stage 1 records and are corrected in S2-4 together with the cleanup-order paragraph.

---

## Self-review

- **Spec coverage.** E1 → Task 1; E2, E4 (adapter) → Task 2; E3, E4 (runtime) → Task 3; F2 → Tasks 4, 5 (Task 4 is the kernel-side enabler the spec's K2 text anticipates: "S2-3のF2はこの2つのfdへ委譲する"); F1 → Task 6; PR conditions (tags, goldens, Family-only suites, CHANGELOG) → Task 7. 変えないもの: no wire, audit, `PROTOCOL_VERSION` value, `StateLayout`, Mount, `podman.py`, readiness, or smoke guide changes anywhere in the plan.
- **Placeholders.** `<base>`, `<ext4 dir outside the repo>`, `<observed>` in Task 7 are filled in at execution from the actual branch point, host, and suite run, as in the S2-1/S2-2 plans. Every code step carries the code.
- **Type consistency.** `Connection(client, stream, peer_uid, peer_pid, peer_gid)` positional order matches `broker/runtime.py`; `admit_connection(client, *, timeout, policy)` keyword names match; `FamilyPeerPolicy(session).admit(connection)` matches `PeerPolicy`; `RuntimeArtifacts.dir_fd`/`parent_dir_fd` (Task 4) are the names Tasks 5 and 6's tests use; `_GATEWAY_CONNECT_TIMEOUT_SECONDS = 30` is the value the Task 3 tests assert; `_CAPABILITY_LABEL`/`_SOCKET_LABEL` strings match the Task 2 regexes.
