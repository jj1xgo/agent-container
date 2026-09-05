from io import BytesIO
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
import threading
import unittest
from unittest import mock

from agent_container.broker.capability import CAPABILITY_PATTERN
from agent_container.broker.runtime import MAX_UNIX_SOCKET_PATH_BYTES
from agent_container.broker.runtime import Connection
from agent_container.broker.runtime import SocketBrokerRuntime
from agent_container.broker.runtime import allocate_run_dir
from agent_container.broker.runtime import bind_private_listener
from agent_container.broker.runtime import create_private_file
from agent_container.broker.runtime import generate_capability
from agent_container.broker.runtime import open_connection
from agent_container.broker.runtime import remove_runtime_artifacts


LABEL = "test broker"


class CreatePrivateFileTest(unittest.TestCase):
    def test_creates_exclusive_private_ascii_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capability"
            create_private_file(path, "abc\n", label=LABEL)
            self.assertEqual(path.read_bytes(), b"abc\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                create_private_file(path, "again\n", label=LABEL)

    def test_mode_parameter_creates_an_owner_read_only_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capability"
            create_private_file(path, "abc\n", label=LABEL, mode=0o400)
            self.assertEqual(path.read_bytes(), b"abc\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)
            default = Path(directory) / "default"
            create_private_file(default, "abc\n", label=LABEL)
            self.assertEqual(stat.S_IMODE(default.stat().st_mode), 0o600)

    def test_refuses_symlink_target_and_short_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                create_private_file(link, "abc\n", label=LABEL)
            self.assertFalse(target.exists())

            path = root / "short"
            with mock.patch("os.write", return_value=0), self.assertRaisesRegex(
                OSError, "test broker private file write failed"
            ):
                create_private_file(path, "abc\n", label=LABEL)


class AllocateRunDirTest(unittest.TestCase):
    def test_allocates_a_private_hex_named_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_id, run_dir = allocate_run_dir(Path(directory), label=LABEL)
            self.assertRegex(run_id, r"^[0-9a-f]{16}$")
            self.assertEqual(run_dir, Path(directory) / run_id)
            self.assertTrue(run_dir.is_dir())
            self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)

    def test_retries_collisions_then_gives_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "aaaaaaaaaaaaaaaa").mkdir(mode=0o700)
            with mock.patch(
                "secrets.token_hex", side_effect=["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"]
            ):
                run_id, run_dir = allocate_run_dir(root, label=LABEL)
            self.assertEqual(run_id, "bbbbbbbbbbbbbbbb")
            self.assertTrue(run_dir.is_dir())

            with mock.patch("secrets.token_hex", return_value="aaaaaaaaaaaaaaaa"), self.assertRaisesRegex(
                FileExistsError, "could not allocate test broker runtime"
            ):
                allocate_run_dir(root, label=LABEL, attempts=3)


class GenerateCapabilityTest(unittest.TestCase):
    def test_generates_43_url_safe_characters(self) -> None:
        capability = generate_capability(label=LABEL)
        self.assertEqual(len(capability), 43)
        self.assertIsNotNone(CAPABILITY_PATTERN.fullmatch(capability))

    def test_rejects_unexpected_token_shape(self) -> None:
        with mock.patch("secrets.token_urlsafe", return_value="short"), self.assertRaisesRegex(
            RuntimeError, "generated test broker capability has invalid format"
        ):
            generate_capability(label=LABEL)


class BindPrivateListenerTest(unittest.TestCase):
    def test_binds_a_private_listening_unix_socket(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl-") as directory:
            path = Path(directory) / "broker.sock"
            listener = bind_private_listener(path, backlog=4, label=LABEL)
            try:
                metadata = path.lstat()
                self.assertTrue(stat.S_ISSOCK(metadata.st_mode))
                self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    client.connect(str(path))
                finally:
                    client.close()
            finally:
                listener.close()

    def test_rejects_long_paths_and_existing_paths(self) -> None:
        self.assertEqual(MAX_UNIX_SOCKET_PATH_BYTES, 107)
        long_path = Path("/" + "a" * 120)
        with self.assertRaisesRegex(ValueError, "test broker socket path is too long"):
            bind_private_listener(long_path, backlog=4, label=LABEL)
        with tempfile.TemporaryDirectory(prefix="bl-") as directory:
            path = Path(directory) / "broker.sock"
            path.write_text("replacement", encoding="ascii")
            with self.assertRaisesRegex(FileExistsError, "test broker socket path already exists"):
                bind_private_listener(path, backlog=4, label=LABEL)

    def test_bind_failure_closes_socket_and_only_unlinks_a_socket(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl-") as directory:
            path = Path(directory) / "broker.sock"
            fake = mock.Mock()
            fake.bind.side_effect = OSError("bind failed")
            with mock.patch("socket.socket", return_value=fake), self.assertRaises(OSError):
                bind_private_listener(path, backlog=4, label=LABEL)
            fake.close.assert_called_once_with()
            self.assertFalse(path.exists())

    def test_chmod_receives_the_path_object(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl-") as directory:
            path = Path(directory) / "broker.sock"
            fake = mock.Mock()
            with mock.patch("socket.socket", return_value=fake), mock.patch("os.chmod") as chmod:
                self.assertIs(bind_private_listener(path, backlog=7, label=LABEL), fake)
            fake.bind.assert_called_once_with(str(path))
            chmod.assert_called_once_with(path, 0o600)
            fake.listen.assert_called_once_with(7)


class RemoveRuntimeArtifactsTest(unittest.TestCase):
    def _layout(self, root: Path) -> tuple[Path, Path, Path]:
        run_dir = root / "run"
        run_dir.mkdir(mode=0o700)
        capability = run_dir / "capability"
        capability.write_text("c" * 43 + "\n", encoding="ascii")
        capability.chmod(0o600)
        return run_dir, capability, run_dir / "broker.sock"

    def test_removes_capability_socket_and_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir, capability, socket_path = self._layout(Path(directory))
            listener = bind_private_listener(socket_path, backlog=1, label=LABEL)
            listener.close()
            failed = remove_runtime_artifacts(
                capability_path=capability, socket_path=socket_path, run_dir=run_dir
            )
            self.assertFalse(failed)
            self.assertFalse(run_dir.exists())

    def test_missing_artifacts_are_not_failures(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir(mode=0o700)
            failed = remove_runtime_artifacts(
                capability_path=run_dir / "capability",
                socket_path=run_dir / "broker.sock",
                run_dir=run_dir,
            )
            self.assertFalse(failed)
            self.assertFalse(run_dir.exists())

    def test_refuses_replaced_capability_and_keeps_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir, capability, socket_path = self._layout(Path(directory))
            capability.unlink()
            capability.mkdir()
            failed = remove_runtime_artifacts(
                capability_path=capability, socket_path=socket_path, run_dir=run_dir
            )
            self.assertTrue(failed)
            self.assertTrue(capability.is_dir())
            self.assertTrue(run_dir.exists())

    def test_refuses_replaced_socket_but_still_removes_capability(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir, capability, socket_path = self._layout(Path(directory))
            socket_path.write_text("replacement", encoding="ascii")
            failed = remove_runtime_artifacts(
                capability_path=capability, socket_path=socket_path, run_dir=run_dir
            )
            self.assertTrue(failed)
            self.assertFalse(capability.exists())
            self.assertTrue(socket_path.is_file())
            self.assertTrue(run_dir.exists())


class RuntimeError_(Exception):
    pass


class FakeStream:
    def __init__(self) -> None:
        self.closed = False
        self.outgoing = BytesIO()

    def read(self, size: int) -> bytes:
        return b""

    def write(self, body: bytes) -> int:
        return self.outgoing.write(body)

    def flush(self) -> None:
        pass

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

    def close(self) -> None:
        self.closed = True


class FakeListener:
    def __init__(self, clients: tuple[FakeClient, ...] = ()) -> None:
        self.clients = list(clients)
        self.timeout: float | None = None
        self.closed = False
        self.accepts = 0
        self._wake = threading.Event()

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def accept(self) -> tuple[FakeClient, None]:
        self.accepts += 1
        if self.clients:
            return self.clients.pop(0), None
        self._wake.wait(0.01)
        raise TimeoutError

    def close(self) -> None:
        self.closed = True
        self._wake.set()


class ManualGate:
    def __init__(self) -> None:
        self.opened = threading.Event()

    def wait(self, timeout: float | None = None) -> bool:
        return self.opened.wait(timeout)


def make_runtime(
    listener: FakeListener,
    handler=None,
    *,
    readiness=None,
    open_listener=None,
    close=None,
    **options,
) -> tuple[SocketBrokerRuntime, dict[str, int]]:
    calls = {"open": 0, "deactivate": 0, "close": 0, "backlog": 0}

    def default_open(backlog: int) -> FakeListener:
        calls["open"] += 1
        calls["backlog"] = backlog
        return listener

    def deactivate() -> None:
        calls["deactivate"] += 1

    def default_close() -> None:
        calls["close"] += 1

    if readiness is not None:
        options["readiness"] = readiness
    runtime = SocketBrokerRuntime(
        label="test broker",
        thread_name="test-broker",
        open_listener=open_listener or default_open,
        handler=handler or (lambda connection: 0),
        deactivate=deactivate,
        close=close or default_close,
        error_type=RuntimeError_,
        backlog=4,
        listener_timeout=0.2,
        client_timeout=30,
        **options,
    )
    return runtime, calls


class SocketBrokerRuntimeTest(unittest.TestCase):
    def test_start_opens_listener_and_runs_a_daemon_thread_until_stop(self) -> None:
        listener = FakeListener()
        runtime, calls = make_runtime(listener)

        runtime.start()
        try:
            self.assertEqual(calls["open"], 1)
            self.assertEqual(calls["backlog"], 4)
            self.assertEqual(listener.timeout, 0.2)
            self.assertIsNotNone(runtime.thread)
            self.assertTrue(runtime.thread.is_alive())
            self.assertTrue(runtime.thread.daemon)
            self.assertEqual(runtime.thread.name, "test-broker")
        finally:
            runtime.stop(join_timeout=2)

        self.assertTrue(listener.closed)
        self.assertTrue(runtime.exited)
        self.assertEqual(calls["deactivate"], 1)
        self.assertEqual(calls["close"], 1)
        self.assertFalse(runtime.thread.is_alive())

    def test_handler_receives_stream_and_peer_uid_and_loop_continues(self) -> None:
        clients = (FakeClient(1010), FakeClient(2020))
        listener = FakeListener(clients)
        handled = threading.Event()
        seen: list[int] = []
        captured: list[Connection] = []

        def handler(connection: Connection) -> int:
            seen.append(connection.peer_uid)
            captured.append(connection)
            if len(seen) == 2:
                handled.set()
            return 0

        runtime, _ = make_runtime(listener, handler)
        runtime.start()
        try:
            self.assertTrue(handled.wait(1))
        finally:
            runtime.stop(join_timeout=2)

        self.assertEqual(seen, [1010, 2020])
        self.assertEqual([c.client for c in captured], list(clients))
        self.assertEqual([c.stream for c in captured], [c.stream for c in clients])
        for client in clients:
            self.assertEqual(
                client.credential_calls, [(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)]
            )
            self.assertEqual(client.timeout, 30)
            self.assertTrue(client.stream.closed)
            self.assertTrue(client.closed)

    def test_start_failure_closes_listener_and_session_and_raises_fixed_message(self) -> None:
        listener = FakeListener()

        def failing_open(backlog: int) -> FakeListener:
            raise OSError("private-start-marker")

        runtime, calls = make_runtime(listener, open_listener=failing_open)
        with self.assertRaises(RuntimeError_) as raised:
            runtime.start()
        self.assertEqual(str(raised.exception), "test broker failed to start")
        self.assertEqual(calls["close"], 1)
        self.assertTrue(runtime.exited)
        with self.assertRaises(RuntimeError_):
            runtime.start()

    def test_start_failure_with_failing_close_allows_retry_through_stop(self) -> None:
        listener = FakeListener()
        close_calls = 0

        def flaky_close() -> None:
            nonlocal close_calls
            close_calls += 1
            if close_calls == 1:
                raise ValueError("private-cleanup-marker")

        class StartFailureThread:
            daemon = True

            def __init__(self, **_: object) -> None:
                self.join_calls = 0

            def start(self) -> None:
                raise RuntimeError("private-thread-start-marker")

            def join(self, timeout: float) -> None:
                self.join_calls += 1

            def is_alive(self) -> bool:
                return False

        thread = StartFailureThread()
        runtime, calls = make_runtime(listener, close=flaky_close)
        with mock.patch("threading.Thread", return_value=thread):
            with self.assertRaises(RuntimeError_) as raised:
                runtime.start()
        self.assertEqual(str(raised.exception), "test broker failed to start")
        self.assertNotIn("private", str(raised.exception))
        self.assertTrue(listener.closed)
        self.assertFalse(runtime.exited)
        self.assertIsNone(runtime.thread)

        runtime.stop(join_timeout=2)
        runtime.stop(join_timeout=2)
        self.assertEqual(thread.join_calls, 0)
        self.assertEqual(close_calls, 2)
        self.assertTrue(runtime.exited)

    def test_handler_exception_is_captured_and_reported_after_cleanup(self) -> None:
        client = FakeClient(os.getuid())
        listener = FakeListener((client,))
        failed = threading.Event()

        def fail(connection: Connection) -> int:
            failed.set()
            raise RuntimeError("private-handler-marker")

        runtime, calls = make_runtime(listener, fail)
        runtime.start()
        self.assertTrue(failed.wait(1))
        with self.assertRaises(RuntimeError_) as raised:
            runtime.stop(join_timeout=2)
        self.assertEqual(str(raised.exception), "test broker failed")
        self.assertNotIn("private-handler-marker", str(raised.exception))
        self.assertEqual(calls["close"], 1)
        self.assertTrue(listener.closed)
        self.assertTrue(runtime.exited)

    def test_stop_timeout_defers_close_until_the_thread_can_be_joined(self) -> None:
        listener = FakeListener()

        class StuckThread:
            daemon = True

            def __init__(self, **_: object) -> None:
                self.join_timeouts: list[float] = []
                self.alive = True

            def start(self) -> None:
                pass

            def join(self, timeout: float) -> None:
                self.join_timeouts.append(timeout)

            def is_alive(self) -> bool:
                return self.alive

        thread = StuckThread()
        runtime, calls = make_runtime(listener)
        with mock.patch("threading.Thread", return_value=thread):
            runtime.start()
        with self.assertRaises(RuntimeError_) as raised:
            runtime.stop(join_timeout=2)
        self.assertEqual(str(raised.exception), "test broker did not stop")
        self.assertEqual(thread.join_timeouts, [2])
        self.assertTrue(listener.closed)
        self.assertEqual(calls["deactivate"], 1)
        self.assertEqual(calls["close"], 0)

        thread.alive = False
        runtime.stop(join_timeout=2)
        self.assertEqual(thread.join_timeouts, [2, 2])
        self.assertEqual(calls["deactivate"], 2)
        self.assertEqual(calls["close"], 1)
        runtime.stop(join_timeout=2)
        self.assertEqual(calls["close"], 1)

    def test_cleanup_failure_is_reported_and_retryable(self) -> None:
        listener = FakeListener()
        close_calls = 0

        def flaky_close() -> None:
            nonlocal close_calls
            close_calls += 1
            if close_calls == 1:
                raise OSError("private-cleanup-marker")

        runtime, _ = make_runtime(listener, close=flaky_close)
        runtime.start()
        with self.assertRaises(RuntimeError_) as raised:
            runtime.stop(join_timeout=2)
        self.assertEqual(str(raised.exception), "test broker cleanup failed")
        self.assertFalse(runtime.exited)
        runtime.stop(join_timeout=2)
        self.assertTrue(runtime.exited)
        self.assertEqual(close_calls, 2)

    def test_readiness_gate_blocks_accept_until_opened(self) -> None:
        client = FakeClient(os.getuid())
        listener = FakeListener((client,))
        gate = ManualGate()
        handled = threading.Event()
        runtime, _ = make_runtime(
            listener, lambda connection: handled.set(), readiness=gate
        )
        runtime.start()
        try:
            self.assertFalse(handled.wait(0.1))
            self.assertEqual(listener.accepts, 0)
            gate.opened.set()
            self.assertTrue(handled.wait(1))
        finally:
            gate.opened.set()
            runtime.stop(join_timeout=2)

    def test_readiness_gate_that_never_opens_still_lets_stop_converge(self) -> None:
        listener = FakeListener((FakeClient(os.getuid()),))
        gate = ManualGate()
        runtime, calls = make_runtime(listener, readiness=gate)
        runtime.start()
        runtime.stop(join_timeout=2)
        self.assertTrue(runtime.exited)
        self.assertIsNone(runtime.error)
        self.assertEqual(listener.accepts, 0)
        self.assertEqual(calls["close"], 1)

    def test_readiness_gate_error_is_reported_as_runtime_failure(self) -> None:
        listener = FakeListener()

        class BrokenGate:
            def wait(self, timeout: float | None = None) -> bool:
                raise RuntimeError("private-gate-marker")

        runtime, _ = make_runtime(listener, readiness=BrokenGate())
        runtime.start()
        with self.assertRaises(RuntimeError_) as raised:
            runtime.stop(join_timeout=2)
        self.assertEqual(str(raised.exception), "test broker failed")
        self.assertNotIn("private-gate-marker", str(raised.exception))
        self.assertEqual(listener.accepts, 0)


class OpenConnectionTest(unittest.TestCase):
    def test_sets_timeout_reads_peer_uid_and_opens_an_unbuffered_stream(self) -> None:
        client = FakeClient(4040)
        connection = open_connection(client, timeout=7)
        self.assertEqual(client.timeout, 7)
        self.assertEqual(
            client.credential_calls, [(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)]
        )
        self.assertEqual(connection, Connection(client, client.stream, 4040))


class ThreadedSocketBrokerRuntimeTest(unittest.TestCase):
    def test_rejects_unknown_concurrency_mode(self) -> None:
        with self.assertRaises(ValueError) as raised:
            make_runtime(FakeListener(), concurrency="process")
        self.assertEqual(str(raised.exception), "test broker concurrency mode is invalid")

    def test_thread_mode_serves_connections_concurrently_on_named_workers(self) -> None:
        clients = (FakeClient(1010), FakeClient(2020))
        listener = FakeListener(clients)
        both_entered = threading.Event()
        release = threading.Event()
        seen: list[tuple[int, str]] = []

        def blocking_handler(connection: Connection) -> None:
            seen.append((connection.peer_uid, threading.current_thread().name))
            if len(seen) == 2:
                both_entered.set()
            release.wait(2)

        runtime, calls = make_runtime(
            listener,
            blocking_handler,
            concurrency="thread",
            worker_thread_name="test-worker",
        )
        runtime.start()
        try:
            self.assertTrue(both_entered.wait(1))
            with runtime.worker_lock:
                self.assertEqual(len(runtime.workers), 2)
        finally:
            release.set()
            runtime.stop(join_timeout=2)

        self.assertEqual(sorted(seen), [(1010, "test-worker"), (2020, "test-worker")])
        self.assertTrue(all(client.closed for client in clients))
        self.assertTrue(all(client.stream.closed for client in clients))
        self.assertEqual(runtime.workers, set())
        self.assertFalse(runtime.wait_failed(0))
        self.assertEqual(calls["deactivate"], 1)
        self.assertEqual(calls["close"], 1)

    def test_thread_mode_default_worker_name_derives_from_thread_name(self) -> None:
        client = FakeClient(1010)
        listener = FakeListener((client,))
        names: list[str] = []
        handled = threading.Event()

        def handler(connection: Connection) -> None:
            names.append(threading.current_thread().name)
            handled.set()

        runtime, _ = make_runtime(listener, handler, concurrency="thread")
        runtime.start()
        try:
            self.assertTrue(handled.wait(1))
        finally:
            runtime.stop(join_timeout=2)
        self.assertEqual(names, ["test-broker-worker"])

    def test_raw_client_hands_the_socket_without_opening_a_connection(self) -> None:
        for concurrency in ("inline", "thread"):
            with self.subTest(concurrency=concurrency):
                client = FakeClient(1010)
                listener = FakeListener((client,))
                received: list[object] = []
                handled = threading.Event()

                def handler(raw: object) -> None:
                    received.append(raw)
                    handled.set()

                runtime, _ = make_runtime(
                    listener, handler, concurrency=concurrency, raw_client=True
                )
                runtime.start()
                try:
                    self.assertTrue(handled.wait(1))
                finally:
                    runtime.stop(join_timeout=2)

                self.assertEqual(received, [client])
                self.assertIsNone(client.timeout)
                self.assertEqual(client.credential_calls, [])
                self.assertFalse(client.stream.closed)
                self.assertTrue(client.closed)

    def test_thread_mode_swallows_os_errors_but_reports_other_worker_failures(self) -> None:
        for error, fatal in (
            (ConnectionResetError("private-client-marker"), False),
            (RuntimeError("private-worker-marker"), True),
        ):
            with self.subTest(fatal=fatal):
                client = FakeClient(1010)
                listener = FakeListener((client,))
                handled = threading.Event()

                def fail(connection: Connection) -> None:
                    handled.set()
                    raise error

                runtime, calls = make_runtime(listener, fail, concurrency="thread")
                runtime.start()
                self.assertTrue(handled.wait(1))
                self.assertEqual(runtime.wait_failed(0.2), fatal)
                self.assertEqual(runtime.stop_event.is_set(), fatal)
                if fatal:
                    with self.assertRaises(RuntimeError_) as raised:
                        runtime.stop(join_timeout=2)
                    self.assertEqual(str(raised.exception), "test broker failed")
                    self.assertNotIn("private-worker-marker", str(raised.exception))
                    self.assertIs(runtime.error, error)
                else:
                    runtime.stop(join_timeout=2)
                    self.assertIsNone(runtime.error)

                self.assertTrue(client.closed)
                self.assertTrue(runtime.exited)
                self.assertEqual(calls["close"], 1)

    def test_accept_loop_failure_sets_failed_in_inline_mode_too(self) -> None:
        class BrokenListener(FakeListener):
            def accept(self):
                raise RuntimeError("private-accept-marker")

        listener = BrokenListener()
        runtime, _ = make_runtime(listener)
        runtime.start()
        self.assertTrue(runtime.wait_failed(1))
        with self.assertRaises(RuntimeError_) as raised:
            runtime.stop(join_timeout=2)
        self.assertEqual(str(raised.exception), "test broker failed")

    def test_deactivate_after_join_runs_deactivate_once_workers_have_finished(self) -> None:
        client = FakeClient(1010)
        listener = FakeListener((client,))
        worker_entered = threading.Event()
        deactivations_seen: list[int] = []

        runtime, calls = make_runtime(
            listener, None, concurrency="thread", deactivate_after_join=True
        )

        def finish_after_listener_close(connection: Connection) -> None:
            worker_entered.set()
            listener._wake.wait(5)
            deactivations_seen.append(calls["deactivate"])

        runtime.handler = finish_after_listener_close
        runtime.start()
        self.assertTrue(worker_entered.wait(1))
        runtime.stop(join_timeout=2)

        self.assertEqual(deactivations_seen, [0])
        self.assertEqual(calls["deactivate"], 1)
        self.assertEqual(calls["close"], 1)
        self.assertTrue(runtime.exited)

    def test_deactivate_after_join_still_deactivates_when_a_worker_does_not_stop(self) -> None:
        client = FakeClient(1010)
        listener = FakeListener((client,))
        release = threading.Event()
        entered = threading.Event()

        def stuck(connection: Connection) -> None:
            entered.set()
            release.wait(5)

        runtime, calls = make_runtime(
            listener, stuck, concurrency="thread", deactivate_after_join=True
        )
        runtime.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaises(RuntimeError_) as raised:
            runtime.stop(join_timeout=0.05)
        self.assertEqual(str(raised.exception), "test broker did not stop")
        self.assertEqual(calls["deactivate"], 1)
        self.assertEqual(calls["close"], 0)
        self.assertFalse(runtime.exited)

        release.set()
        runtime.stop(join_timeout=2)
        self.assertEqual(calls["close"], 1)
        self.assertTrue(runtime.exited)
        self.assertTrue(client.closed)

    def test_deactivate_before_join_is_still_the_default(self) -> None:
        client = FakeClient(1010)
        listener = FakeListener((client,))
        worker_entered = threading.Event()
        deactivations_seen: list[int] = []

        runtime, calls = make_runtime(listener, None, concurrency="thread")

        def observe(connection: Connection) -> None:
            worker_entered.set()
            listener._wake.wait(5)
            deactivations_seen.append(calls["deactivate"])

        runtime.handler = observe
        runtime.start()
        self.assertTrue(worker_entered.wait(1))
        runtime.stop(join_timeout=2)
        self.assertEqual(deactivations_seen, [1])


if __name__ == "__main__":
    unittest.main()
