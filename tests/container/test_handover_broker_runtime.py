import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from unittest import mock

from agent_container.handover_broker import HandoverBrokerSession
from agent_container.handover_broker_protocol import HandoverRequest
from agent_container.handover_broker_runtime import HandoverBrokerRuntime
from agent_container.handover_broker_runtime import HandoverBrokerRuntimeError
from agent_container.handover_broker_runtime import HandoverRuntimeMount
from agent_container.state import StateLayout


VALID_BODY = """## 作業の目的
目的
## 現在地
現在地
## 決定事項と理由
決定
## 変更したファイル・commit・PR
変更
## 検証結果
検証
## 未解決事項とリスク
リスク
## 次の一手
次
"""


class FakeStream:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self, peer_uid: int) -> None:
        self.peer_uid = peer_uid
        self.timeout: float | None = None
        self.credential_calls: list[tuple[int, int, int]] = []
        self.stream = FakeStream()
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def getsockopt(self, level: int, option: int, size: int) -> bytes:
        self.credential_calls.append((level, option, size))
        return struct.pack("3i", 1234, self.peer_uid, 5678)

    def makefile(self, *_: object, **__: object) -> FakeStream:
        return self.stream

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.closed = True


class FakeListener:
    def __init__(self, clients: tuple[FakeClient, ...] = ()) -> None:
        self.clients = list(clients)
        self.timeout: float | None = None
        self.closed = False
        self._wake = threading.Event()

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def accept(self) -> tuple[FakeClient, None]:
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
        self.socket_path = self.run_dir / "broker.sock"
        self.capability_path = self.run_dir / "capability"
        self.listener = listener
        self.backlogs: list[int] = []
        self.close_calls = 0

    def open_listener(self, backlog: int = 4) -> FakeListener:
        self.backlogs.append(backlog)
        return self.listener

    def close(self) -> None:
        self.close_calls += 1


class FailingStartSession(FakeSession):
    def open_listener(self, backlog: int = 4) -> FakeListener:
        self.backlogs.append(backlog)
        raise OSError("private-start-marker")


class FailingStartRetryCloseSession(FailingStartSession):
    def close(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise ValueError("private-cleanup-marker")


class HandoverBrokerRuntimeTest(unittest.TestCase):
    def test_mount_derives_only_runtime_socket_and_capability_paths(self) -> None:
        mount = HandoverRuntimeMount(Path("/private/runtime"))

        self.assertEqual(mount.socket_path, Path("/private/runtime/broker.sock"))
        self.assertEqual(mount.capability_path, Path("/private/runtime/capability"))

    def test_create_binds_layout_project_and_project_directory(self) -> None:
        layout = StateLayout(Path("/private/state"), "agent-container")
        project = Path("/private/handovers/agent-container")
        session = FakeSession(FakeListener())

        with mock.patch(
            "agent_container.handover_broker_runtime.HandoverBrokerSession.create",
            return_value=session,
        ) as create:
            runtime = HandoverBrokerRuntime.create(layout, project)

        self.assertIs(runtime.session, session)
        create.assert_called_once_with(layout.root, layout.project_id, project)

    def test_enter_returns_only_after_listener_and_daemon_thread_are_running(self) -> None:
        listener = FakeListener()
        session = FakeSession(listener)
        runtime = HandoverBrokerRuntime(session)  # type: ignore[arg-type]

        with runtime as mount:
            self.assertEqual(mount, HandoverRuntimeMount(session.run_dir))
            self.assertEqual(session.backlogs, [4])
            self.assertEqual(listener.timeout, 0.2)
            self.assertIsNotNone(runtime._thread)
            self.assertTrue(runtime._thread.is_alive())  # type: ignore[union-attr]
            self.assertTrue(runtime._thread.daemon)  # type: ignore[union-attr]

        self.assertTrue(listener.closed)
        self.assertEqual(session.close_calls, 1)

    def test_reads_peer_credentials_and_continues_after_denied_connection(self) -> None:
        clients = (FakeClient(1010), FakeClient(2020))
        listener = FakeListener(clients)
        session = FakeSession(listener)
        runtime = HandoverBrokerRuntime(session)  # type: ignore[arg-type]
        handled = threading.Event()
        peer_uids: list[int] = []

        def handler(
            received_session: object,
            stream: FakeStream,
            peer_uid: int,
        ) -> int:
            self.assertIs(received_session, session)
            peer_uids.append(peer_uid)
            if len(peer_uids) == 2:
                handled.set()
            return 1 if len(peer_uids) == 1 else 0

        with mock.patch(
            "agent_container.handover_broker_runtime.handle_handover_connection",
            side_effect=handler,
        ):
            with runtime:
                self.assertTrue(handled.wait(1))

        self.assertEqual(peer_uids, [1010, 2020])
        for client in clients:
            self.assertEqual(
                client.credential_calls,
                [(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)],
            )
            self.assertIsNotNone(client.timeout)
            self.assertGreater(client.timeout or 0, 0)
            self.assertLessEqual(client.timeout or 31, 30)
            self.assertTrue(client.stream.closed)
            self.assertTrue(client.closed)

    def test_start_failure_cleans_session_and_raises_secret_free_error(self) -> None:
        session = FailingStartSession(FakeListener())
        runtime = HandoverBrokerRuntime(session)  # type: ignore[arg-type]

        with self.assertRaises(HandoverBrokerRuntimeError) as raised:
            runtime.__enter__()

        self.assertEqual(str(raised.exception), "handover broker failed to start")
        self.assertNotIn("private-start-marker", str(raised.exception))
        self.assertEqual(session.close_calls, 1)

    def test_start_failure_allows_failed_session_cleanup_to_be_retried(self) -> None:
        session = FailingStartRetryCloseSession(FakeListener())
        runtime = HandoverBrokerRuntime(session)  # type: ignore[arg-type]

        with self.assertRaises(HandoverBrokerRuntimeError) as raised:
            runtime.__enter__()

        self.assertEqual(str(raised.exception), "handover broker failed to start")
        self.assertNotIn("private-cleanup-marker", str(raised.exception))
        runtime.__exit__(None, None, None)
        runtime.__exit__(None, None, None)
        self.assertEqual(session.close_calls, 2)

    def test_thread_start_and_first_cleanup_failure_still_allow_safe_retry(
        self,
    ) -> None:
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
            old_capability = session._capability
            request = HandoverRequest(
                1,
                old_capability,
                "agent-container",
                "create",
                "Safe title",
                VALID_BODY,
            )
            listener = FakeListener()
            real_close = session.close
            close_calls = 0

            def fail_first_close() -> None:
                nonlocal close_calls
                close_calls += 1
                if close_calls == 1:
                    raise ValueError("private-cleanup-marker")
                real_close()

            class StartFailureThread:
                daemon = True

                def __init__(self, **_: object) -> None:
                    self.join_calls = 0

                def start(self) -> None:
                    raise RuntimeError("private-thread-start-marker")

                def join(self, timeout: float) -> None:
                    self.join_calls += 1
                    raise RuntimeError("cannot join thread before it is started")

                def is_alive(self) -> bool:
                    return False

            thread = StartFailureThread()
            with mock.patch.object(
                session,
                "open_listener",
                return_value=listener,
            ), mock.patch.object(
                session,
                "close",
                side_effect=fail_first_close,
            ), mock.patch(
                "agent_container.handover_broker_runtime.threading.Thread",
                return_value=thread,
            ):
                runtime = HandoverBrokerRuntime(session)
                with self.assertRaises(HandoverBrokerRuntimeError) as raised:
                    runtime.__enter__()
                self.assertEqual(
                    str(raised.exception),
                    "handover broker failed to start",
                )

                runtime.__exit__(None, None, None)

            self.assertEqual(thread.join_calls, 0)
            self.assertEqual(close_calls, 2)
            self.assertFalse(session.capability_path.exists())
            self.assertFalse(session.run_dir.exists())
            with self.assertRaises(ValueError):
                session.authorize(request, os.getuid())

    def test_handler_exception_is_captured_and_cleanup_precedes_fixed_error(self) -> None:
        client = FakeClient(os.getuid())
        session = FakeSession(FakeListener((client,)))
        runtime = HandoverBrokerRuntime(session)  # type: ignore[arg-type]
        failed = threading.Event()

        def fail(*_: object) -> int:
            failed.set()
            raise RuntimeError("private-handler-marker")

        with mock.patch(
            "agent_container.handover_broker_runtime.handle_handover_connection",
            side_effect=fail,
        ), self.assertRaises(HandoverBrokerRuntimeError) as raised:
            with runtime:
                self.assertTrue(failed.wait(1))

        self.assertEqual(str(raised.exception), "handover broker failed")
        self.assertNotIn("private-handler-marker", str(raised.exception))
        self.assertEqual(session.close_calls, 1)
        self.assertTrue(session.listener.closed)

    def test_handler_failure_during_shutdown_is_not_misclassified_as_listener_close(
        self,
    ) -> None:
        client = FakeClient(os.getuid())
        session = FakeSession(FakeListener((client,)))
        runtime = HandoverBrokerRuntime(session)  # type: ignore[arg-type]
        entered_handler = threading.Event()

        def fail_after_stop(*_: object) -> int:
            entered_handler.set()
            self.assertTrue(runtime._stop.wait(1))
            raise RuntimeError("private-shutdown-marker")

        with mock.patch(
            "agent_container.handover_broker_runtime.handle_handover_connection",
            side_effect=fail_after_stop,
        ):
            runtime.__enter__()
            self.assertTrue(entered_handler.wait(1))
            with self.assertRaises(HandoverBrokerRuntimeError) as raised:
                runtime.__exit__(None, None, None)

        self.assertEqual(str(raised.exception), "handover broker failed")
        self.assertNotIn("private-shutdown-marker", str(raised.exception))
        self.assertEqual(session.close_calls, 1)

    def test_stop_timeout_still_closes_listener_and_session_once(self) -> None:
        listener = FakeListener()
        session = FakeSession(listener)

        class StuckThread:
            daemon = True

            def __init__(self, **_: object) -> None:
                self.join_timeouts: list[float] = []

            def start(self) -> None:
                pass

            def join(self, timeout: float) -> None:
                self.join_timeouts.append(timeout)

            def is_alive(self) -> bool:
                return True

        thread = StuckThread()
        with mock.patch(
            "agent_container.handover_broker_runtime.threading.Thread",
            return_value=thread,
        ):
            runtime = HandoverBrokerRuntime(session)  # type: ignore[arg-type]
            runtime.__enter__()
            with self.assertRaises(HandoverBrokerRuntimeError) as raised:
                runtime.__exit__(None, None, None)

        self.assertEqual(str(raised.exception), "handover broker did not stop")
        self.assertEqual(thread.join_timeouts, [2])
        self.assertTrue(listener.closed)
        self.assertEqual(session.close_calls, 1)

        runtime.__exit__(None, None, None)
        self.assertEqual(session.close_calls, 1)

    def test_exit_invalidates_capability_and_new_runtime_does_not_reuse_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hb-runtime-") as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir(mode=0o700)
            handovers = root / "handovers"
            handovers.mkdir(mode=0o700)
            project = handovers / "agent-container"
            project.mkdir(mode=0o700)
            layout = StateLayout(state.resolve(), "agent-container")
            first = HandoverBrokerRuntime.create(layout, project.resolve())
            old_capability = first.session._capability
            request = HandoverRequest(
                1,
                old_capability,
                "agent-container",
                "create",
                "Safe title",
                VALID_BODY,
            )
            first_listener = FakeListener()

            with mock.patch.object(
                first.session,
                "open_listener",
                return_value=first_listener,
            ):
                with first as mount:
                    old_capability_path = mount.capability_path

            self.assertFalse(old_capability_path.exists())
            with self.assertRaises(ValueError):
                first.session.authorize(request, os.getuid())

            second = HandoverBrokerRuntime.create(layout, project.resolve())
            try:
                self.assertNotEqual(second.session._capability, old_capability)
                self.assertNotEqual(second.session.run_dir, first.session.run_dir)
            finally:
                second.session.close()


if __name__ == "__main__":
    unittest.main()
