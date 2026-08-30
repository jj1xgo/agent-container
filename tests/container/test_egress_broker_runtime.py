from io import BytesIO
import os
from pathlib import Path
import socket
import struct
import threading
import unittest
from unittest import mock

from agent_container.egress_broker_runtime import EgressBrokerRuntime
from agent_container.egress_broker_runtime import EgressBrokerRuntimeError
from agent_container.egress_broker_runtime import EgressRuntimeMount
from agent_container.egress_broker_protocol import decode_response_frame
from agent_container.egress_broker_protocol import EgressRequest
from agent_container.egress_broker_protocol import encode_request_frame
from agent_container.egress_gateway import RelayCounts
from agent_container.egress_gateway import ResolvedTarget
from agent_container.egress_policy import EgressPolicy
from agent_container.state import StateLayout


class FakeListener:
    def __init__(self, clients: tuple[object, ...] = ()) -> None:
        self.clients = list(clients)
        self.timeout: float | None = None
        self.closed = False
        self._wake = threading.Event()

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def accept(self):
        if self.clients:
            return self.clients.pop(0), None
        self._wake.wait(0.01)
        raise TimeoutError

    def close(self) -> None:
        self.closed = True
        self._wake.set()


class FakeSession:
    def __init__(self, listener: FakeListener) -> None:
        self.run_dir = Path("/private/runtime")
        self.project_id = "demo"
        self.agent = "codex"
        self.listener = listener
        self.backlogs = []
        self.deactivate_calls = 0
        self.close_calls = 0

    def open_listener(self, backlog: int = 4) -> FakeListener:
        self.backlogs.append(backlog)
        return self.listener

    def deactivate(self) -> None:
        self.deactivate_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class FakeStream:
    def __init__(self, incoming: bytes) -> None:
        self.incoming = BytesIO(incoming)
        self.outgoing = BytesIO()
        self.closed = False

    def read(self, size: int) -> bytes:
        return self.incoming.read(size)

    def write(self, body: bytes) -> int:
        return self.outgoing.write(body)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self, stream: FakeStream, peer_uid: int = os.getuid()) -> None:
        self.stream = stream
        self.peer_uid = peer_uid
        self.timeout: float | None = None
        self.credential_calls = []
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def getsockopt(self, level: int, option: int, size: int) -> bytes:
        self.credential_calls.append((level, option, size))
        return struct.pack("3i", 1234, self.peer_uid, 5678)

    def makefile(self, *_: object, **__: object) -> FakeStream:
        return self.stream

    def close(self) -> None:
        self.closed = True


class ProcessingSession(FakeSession):
    def __init__(self) -> None:
        super().__init__(FakeListener())
        self.authorized = []
        self.audits = []

    def authorize(self, request: EgressRequest, peer_uid: int) -> str:
        self.authorized.append((request, peer_uid))
        if request.capability != "safe-capability":
            raise ValueError("egress broker request is not authorized")
        if request.domain == "denied.example.com":
            error = ValueError("egress broker request domain is not allowed")
            error.stage = "policy"  # type: ignore[attr-defined]
            raise error
        return request.domain

    def audit(self, status: str, **values: object) -> None:
        self.audits.append((status, values))


class EgressBrokerRuntimeTest(unittest.TestCase):
    def request(self, **changes: object) -> EgressRequest:
        baseline = {
            "version": 1,
            "capability": "safe-capability",
            "project_id": "demo",
            "sequence": 1,
            "operation": "connect",
            "domain": "example.com",
            "port": 443,
        }
        return EgressRequest(**(baseline | changes))  # type: ignore[arg-type]

    def test_mount_exposes_only_runtime_socket_and_capability(self) -> None:
        mount = EgressRuntimeMount(Path("/private/runtime"), "demo", "codex")
        self.assertEqual(mount.socket_path, Path("/private/runtime/broker.sock"))
        self.assertEqual(mount.capability_path, Path("/private/runtime/capability"))

    def test_create_binds_layout_agent_and_policy(self) -> None:
        layout = StateLayout(Path("/private/state"), "demo")
        policy = EgressPolicy(1, "allowlist", ("example.com",))
        session = FakeSession(FakeListener())
        with mock.patch(
            "agent_container.egress_broker_runtime.EgressBrokerSession.create",
            return_value=session,
        ) as create:
            runtime = EgressBrokerRuntime.create(layout, "codex", policy)

        self.assertIs(runtime.session, session)
        create.assert_called_once_with(layout, "codex", policy)

    def test_context_starts_listener_and_cleans_exact_session(self) -> None:
        listener = FakeListener()
        session = FakeSession(listener)
        runtime = EgressBrokerRuntime(session)  # type: ignore[arg-type]

        with runtime as mount:
            self.assertEqual(
                mount,
                EgressRuntimeMount(session.run_dir, session.project_id, session.agent),
            )
            self.assertEqual(session.backlogs, [32])
            self.assertEqual(listener.timeout, 0.2)
            self.assertIsNotNone(runtime._thread)
            self.assertTrue(runtime._thread.is_alive())  # type: ignore[union-attr]

        self.assertTrue(listener.closed)
        self.assertEqual(session.deactivate_calls, 1)
        self.assertEqual(session.close_calls, 1)

    def test_reservations_enforce_active_and_lifetime_limits(self) -> None:
        runtime = EgressBrokerRuntime(FakeSession(FakeListener()))  # type: ignore[arg-type]
        reservations = [runtime._reserve_tunnel() for _ in range(32)]
        with self.assertRaisesRegex(ValueError, "limit"):
            runtime._reserve_tunnel()

        concurrent = EgressBrokerRuntime(FakeSession(FakeListener()))  # type: ignore[arg-type]
        for _ in range(127):
            reservation = concurrent._reserve_tunnel()
            concurrent._mark_tunnel_created(reservation)
            concurrent._release_tunnel(reservation)
        final_slot = concurrent._reserve_tunnel()
        with self.assertRaisesRegex(ValueError, "limit"):
            concurrent._reserve_tunnel()
        concurrent._release_tunnel(final_slot)
        for reservation in reservations:
            runtime._mark_tunnel_created(reservation)
            runtime._release_tunnel(reservation)

        for _ in range(96):
            reservation = runtime._reserve_tunnel()
            runtime._mark_tunnel_created(reservation)
            runtime._release_tunnel(reservation)
        with self.assertRaisesRegex(ValueError, "limit"):
            runtime._reserve_tunnel()

    def test_start_failure_cleans_and_raises_fixed_error(self) -> None:
        class FailingSession(FakeSession):
            def open_listener(self, backlog: int = 4):
                raise OSError("private-start-marker")

        session = FailingSession(FakeListener())
        runtime = EgressBrokerRuntime(session)  # type: ignore[arg-type]
        with self.assertRaises(EgressBrokerRuntimeError) as raised:
            runtime.__enter__()

        self.assertEqual(str(raised.exception), "egress broker failed to start")
        self.assertNotIn("private-start-marker", str(raised.exception))
        self.assertEqual(session.close_calls, 1)

    def test_connection_authenticates_resolves_relays_and_audits_counts(self) -> None:
        session = ProcessingSession()
        runtime = EgressBrokerRuntime(session)  # type: ignore[arg-type]
        stream = FakeStream(encode_request_frame(self.request()))
        client = FakeClient(stream)
        target = ResolvedTarget(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            ("93.184.216.34", 443),
        )
        upstream = mock.Mock()

        with mock.patch(
            "agent_container.egress_broker_runtime.resolve_target",
            return_value=(target,),
        ), mock.patch(
            "agent_container.egress_broker_runtime.connect_target",
            return_value=upstream,
        ), mock.patch(
            "agent_container.egress_broker_runtime.relay_tunnel",
            return_value=RelayCounts(12, 34),
        ):
            runtime._handle_client(client)  # type: ignore[arg-type]

        response, consumed = decode_response_frame(stream.outgoing.getvalue())
        self.assertEqual(consumed, len(stream.outgoing.getvalue()))
        self.assertEqual((response.status, response.code), ("ok", "connect"))
        self.assertEqual(session.authorized[0][1], os.getuid())
        self.assertEqual(
            client.credential_calls,
            [(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)],
        )
        self.assertEqual(
            session.audits,
            [
                (
                    "ok",
                    {"bytes_from_client": 12, "bytes_from_upstream": 34},
                )
            ],
        )
        upstream.close.assert_called_once_with()
        self.assertEqual(runtime._created_tunnels, 1)
        self.assertEqual(runtime._active_reservations, set())

    def test_denied_authentication_returns_fixed_response_without_network(self) -> None:
        session = ProcessingSession()
        runtime = EgressBrokerRuntime(session)  # type: ignore[arg-type]
        marker = "private-capability-marker"
        stream = FakeStream(
            encode_request_frame(self.request(capability=marker))
        )
        client = FakeClient(stream)

        with mock.patch(
            "agent_container.egress_broker_runtime.resolve_target"
        ) as resolve:
            runtime._handle_client(client)  # type: ignore[arg-type]

        response, _ = decode_response_frame(stream.outgoing.getvalue())
        self.assertEqual((response.status, response.code), ("denied", "authentication"))
        self.assertNotIn(marker, stream.outgoing.getvalue().decode("ascii"))
        resolve.assert_not_called()
        self.assertEqual(session.audits, [("denied", {"stage": "authentication"})])
        self.assertEqual(runtime._active_reservations, set())

    def test_policy_denial_is_distinct_and_auth_does_not_observe_capacity(self) -> None:
        session = ProcessingSession()
        runtime = EgressBrokerRuntime(session)  # type: ignore[arg-type]
        held = [runtime._reserve_tunnel() for _ in range(32)]
        try:
            auth_stream = FakeStream(
                encode_request_frame(
                    self.request(capability="private-capability-marker")
                )
            )
            runtime._handle_client(FakeClient(auth_stream))  # type: ignore[arg-type]
            auth_response, _ = decode_response_frame(auth_stream.outgoing.getvalue())
            self.assertEqual(
                (auth_response.status, auth_response.code),
                ("denied", "authentication"),
            )
        finally:
            for reservation in held:
                runtime._release_tunnel(reservation)

        policy_stream = FakeStream(
            encode_request_frame(self.request(domain="denied.example.com"))
        )
        runtime._handle_client(FakeClient(policy_stream))  # type: ignore[arg-type]
        policy_response, _ = decode_response_frame(policy_stream.outgoing.getvalue())
        self.assertEqual(
            (policy_response.status, policy_response.code),
            ("denied", "policy"),
        )
        self.assertEqual(
            session.audits[-1], ("denied", {"stage": "policy"})
        )

    def test_listener_dispatches_connections_concurrently(self) -> None:
        clients = (
            FakeClient(FakeStream(b"")),
            FakeClient(FakeStream(b"")),
        )
        listener = FakeListener(clients)
        session = FakeSession(listener)
        runtime = EgressBrokerRuntime(session)  # type: ignore[arg-type]
        both_entered = threading.Event()
        release = threading.Event()
        entered = []

        def blocking_handler(client: FakeClient) -> None:
            entered.append(client)
            if len(entered) == 2:
                both_entered.set()
            release.wait(2)

        try:
            with mock.patch.object(runtime, "_handle_client", side_effect=blocking_handler):
                runtime.__enter__()
                self.assertTrue(both_entered.wait(1))
        finally:
            release.set()
            runtime.__exit__(None, None, None)

        self.assertEqual(entered, list(clients))
        self.assertTrue(all(client.closed for client in clients))

    def test_client_disconnect_is_graceful_but_worker_bug_is_fatal(self) -> None:
        for error, fatal in (
            (ConnectionResetError("private-client-marker"), False),
            (RuntimeError("private-worker-marker"), True),
        ):
            with self.subTest(fatal=fatal):
                client = FakeClient(FakeStream(b""))
                listener = FakeListener((client,))
                session = FakeSession(listener)
                runtime = EgressBrokerRuntime(session)  # type: ignore[arg-type]
                handled = threading.Event()

                def fail(_: FakeClient) -> None:
                    handled.set()
                    raise error

                with mock.patch.object(runtime, "_handle_client", side_effect=fail):
                    runtime.__enter__()
                    self.assertTrue(handled.wait(1))
                    self.assertEqual(runtime.wait_failed(0.1), fatal)
                    if fatal:
                        with self.assertRaises(EgressBrokerRuntimeError) as raised:
                            runtime.__exit__(None, None, None)
                        self.assertEqual(
                            str(raised.exception), "egress broker failed"
                        )
                        self.assertNotIn("private-worker-marker", str(raised.exception))
                    else:
                        runtime.__exit__(None, None, None)

                self.assertTrue(client.closed)
                self.assertEqual(session.close_calls, 1)

    def test_connection_failures_are_stage_fixed_and_secret_free(self) -> None:
        target = ResolvedTarget(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            ("93.184.216.34", 443),
        )
        cases = (
            ("resolve", "denied", "resolve"),
            ("connect", "error", "connect"),
            ("relay", "ok", "connect"),
        )
        for stage, status, code in cases:
            with self.subTest(stage=stage):
                session = ProcessingSession()
                runtime = EgressBrokerRuntime(session)  # type: ignore[arg-type]
                stream = FakeStream(encode_request_frame(self.request()))
                client = FakeClient(stream)
                upstream = mock.Mock()
                resolve_effect = (
                    ValueError("private-domain-marker")
                    if stage == "resolve"
                    else None
                )
                connect_effect = (
                    ValueError("private-address-marker")
                    if stage == "connect"
                    else None
                )
                relay_effect = (
                    ValueError("private-relay-marker")
                    if stage == "relay"
                    else None
                )
                with mock.patch(
                    "agent_container.egress_broker_runtime.resolve_target",
                    return_value=(target,),
                    side_effect=resolve_effect,
                ), mock.patch(
                    "agent_container.egress_broker_runtime.connect_target",
                    return_value=upstream,
                    side_effect=connect_effect,
                ), mock.patch(
                    "agent_container.egress_broker_runtime.relay_tunnel",
                    return_value=RelayCounts(1, 1),
                    side_effect=relay_effect,
                ):
                    runtime._handle_client(client)  # type: ignore[arg-type]

                response, _ = decode_response_frame(stream.outgoing.getvalue())
                self.assertEqual((response.status, response.code), (status, code))
                rendered = stream.outgoing.getvalue().decode("ascii")
                self.assertNotIn("private-", rendered)
                expected_status = "error" if stage in {"connect", "relay"} else "denied"
                self.assertEqual(
                    session.audits,
                    [(expected_status, {"stage": stage})],
                )
                self.assertEqual(runtime._active_reservations, set())

    def test_shutdown_allows_inflight_worker_to_finish_final_audit(self) -> None:
        client = FakeClient(FakeStream(b""))
        listener = FakeListener((client,))

        class AuditSession(FakeSession):
            def __init__(self) -> None:
                super().__init__(listener)
                self.audit_calls = 0

            def audit(self) -> None:
                if self.deactivate_calls:
                    raise ValueError("session closed before final audit")
                self.audit_calls += 1

        session = AuditSession()
        runtime = EgressBrokerRuntime(session)  # type: ignore[arg-type]
        worker_entered = threading.Event()

        def finish_after_listener_close(_: FakeClient) -> None:
            worker_entered.set()
            self.assertTrue(listener._wake.wait(1))
            session.audit()

        with mock.patch.object(
            runtime, "_handle_client", side_effect=finish_after_listener_close
        ):
            runtime.__enter__()
            self.assertTrue(worker_entered.wait(1))
            runtime.__exit__(None, None, None)

        self.assertEqual(session.audit_calls, 1)
        self.assertEqual(session.deactivate_calls, 1)
        self.assertEqual(session.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
