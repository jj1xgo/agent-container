from io import BytesIO, StringIO
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from agent_container.handover_broker_client import HandoverBrokerClient
from agent_container.handover_broker_client import read_handover_capability
from agent_container.handover_broker_client import run
from agent_container.handover_broker_client import validate_handover_socket
from agent_container.handover_broker_protocol import HandoverResponse
from agent_container.handover_broker_protocol import MAX_REQUEST_BYTES
from agent_container.handover_broker_protocol import decode_request_frame
from agent_container.handover_broker_protocol import encode_response_frame


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
""".encode()


class Duplex:
    def __init__(self, incoming: bytes) -> None:
        self.incoming = BytesIO(incoming)
        self.outgoing = BytesIO()
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self.incoming.read(size)

    def write(self, body: bytes) -> int:
        return self.outgoing.write(body)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeSocket:
    def __init__(self, stream: Duplex, *, connect_error: OSError | None = None) -> None:
        self.stream = stream
        self.connect_error = connect_error
        self.timeout: float | None = None
        self.connected: str | None = None
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def connect(self, path: str) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = path

    def makefile(self, *_: object, **__: object) -> Duplex:
        return self.stream

    def close(self) -> None:
        self.closed = True


class HandoverBrokerRuntimePathTest(unittest.TestCase):
    def test_reads_only_exact_private_current_user_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capability = root / "capability"
            capability.write_text("c" * 43 + "\n", encoding="ascii")
            capability.chmod(0o600)

            self.assertEqual(read_handover_capability(capability.resolve()), "c" * 43)

            link = root / "link"
            link.symlink_to(capability)
            invalid_paths = (Path("capability"), link)
            for path in invalid_paths:
                with self.subTest(path=path), self.assertRaises(ValueError):
                    read_handover_capability(path)

            capability.chmod(0o644)
            with self.assertRaises(ValueError):
                read_handover_capability(capability.resolve())
            capability.chmod(0o600)
            with mock.patch(
                "agent_container.handover_broker_client.os.getuid",
                return_value=os.getuid() + 1,
            ), self.assertRaises(ValueError):
                read_handover_capability(capability.resolve())

            directory_path = root / "directory"
            directory_path.mkdir(mode=0o700)
            with self.assertRaises(ValueError):
                read_handover_capability(directory_path.resolve())

    def test_socket_requires_exact_private_current_user_socket(self) -> None:
        path = Path("/run/agent-handover/broker.sock")
        valid = os.stat_result(
            (stat.S_IFSOCK | 0o600, 0, 0, 1, os.getuid(), 0, 0, 0, 0, 0)
        )
        invalid = (
            os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, os.getuid(), 0, 0, 0, 0, 0)),
            os.stat_result((stat.S_IFSOCK | 0o660, 0, 0, 1, os.getuid(), 0, 0, 0, 0, 0)),
            os.stat_result((stat.S_IFSOCK | 0o600, 0, 0, 1, os.getuid() + 1, 0, 0, 0, 0, 0)),
        )
        with mock.patch(
            "agent_container.handover_broker_client._validate_exact_path",
            return_value=path,
        ), mock.patch.object(Path, "stat", return_value=valid):
            self.assertEqual(validate_handover_socket(path), path)
        for metadata in invalid:
            with self.subTest(mode=metadata.st_mode, uid=metadata.st_uid), mock.patch(
                "agent_container.handover_broker_client._validate_exact_path",
                return_value=path,
            ), mock.patch.object(Path, "stat", return_value=metadata), self.assertRaises(
                ValueError
            ):
                validate_handover_socket(path)


class HandoverBrokerClientTest(unittest.TestCase):
    @mock.patch(
        "agent_container.handover_broker_client.read_handover_capability",
        return_value="c" * 43,
    )
    @mock.patch("agent_container.handover_broker_client.validate_handover_socket")
    def test_create_sends_one_fixed_project_request_and_returns_only_path(
        self,
        validate_socket: mock.Mock,
        read_capability: mock.Mock,
    ) -> None:
        expected = "/handovers/agent-container/2026-08-27_123456_abcdef12.md"
        stream = Duplex(
            encode_response_frame(HandoverResponse(1, "ok", expected, ""))
        )
        socket_client = FakeSocket(stream)
        client = HandoverBrokerClient(
            socket_path=Path("/run/agent-handover/broker.sock"),
            capability_path=Path("/run/agent-handover/capability"),
            project_id="agent-container",
            socket_factory=lambda *_: socket_client,
        )

        path = client.create("Safe title", VALID_BODY)

        request, consumed = decode_request_frame(stream.outgoing.getvalue())
        self.assertEqual(path, expected)
        self.assertEqual(consumed, len(stream.outgoing.getvalue()))
        self.assertEqual(request.project_id, "agent-container")
        self.assertEqual(request.operation, "create")
        self.assertEqual(request.title, "Safe title")
        self.assertEqual(request.body.encode(), VALID_BODY)
        self.assertEqual(socket_client.connected, "/run/agent-handover/broker.sock")
        self.assertIsNotNone(socket_client.timeout)
        self.assertGreater(socket_client.timeout or 0, 0)
        self.assertLessEqual(socket_client.timeout or 61, 60)
        self.assertTrue(stream.closed)
        self.assertTrue(socket_client.closed)
        validate_socket.assert_called_once_with(Path("/run/agent-handover/broker.sock"))
        read_capability.assert_called_once_with(Path("/run/agent-handover/capability"))

    @mock.patch(
        "agent_container.handover_broker_client.read_handover_capability",
        return_value="private-capability-marker",
    )
    @mock.patch("agent_container.handover_broker_client.validate_handover_socket")
    def test_denied_error_and_unavailable_fail_closed_without_secret_echo(
        self,
        _: mock.Mock,
        __: mock.Mock,
    ) -> None:
        responses = (
            HandoverResponse(1, "denied", "", "authentication"),
            HandoverResponse(1, "error", "", "write"),
        )
        for response in responses:
            with self.subTest(status=response.status, code=response.code):
                socket_client = FakeSocket(Duplex(encode_response_frame(response)))
                client = HandoverBrokerClient(
                    Path("/run/agent-handover/broker.sock"),
                    Path("/run/agent-handover/capability"),
                    "agent-container",
                    socket_factory=lambda *_: socket_client,
                )
                with self.assertRaises(RuntimeError) as raised:
                    client.create("private-title-marker", VALID_BODY)
                self.assertNotIn("private-capability-marker", str(raised.exception))
                self.assertNotIn("private-title-marker", str(raised.exception))
                self.assertNotIn(response.code, str(raised.exception))
                self.assertTrue(socket_client.closed)

        unavailable = FakeSocket(
            Duplex(b""), connect_error=FileNotFoundError("private-socket-marker")
        )
        client = HandoverBrokerClient(
            Path("/run/agent-handover/broker.sock"),
            Path("/run/agent-handover/capability"),
            "agent-container",
            socket_factory=lambda *_: unavailable,
        )
        with self.assertRaises(OSError) as raised:
            client.create("private-title-marker", VALID_BODY)
        self.assertNotIn("private-title-marker", str(raised.exception))
        self.assertTrue(unavailable.closed)

    def test_cli_bounds_stdin_requires_environment_and_prints_fixed_errors(self) -> None:
        class UnexpectedClient:
            def __init__(self, **_: object) -> None:
                raise AssertionError("client must not be constructed")

        required = {
            "AGENT_HANDOVER_BROKER_SOCKET": "/run/agent-handover/broker.sock",
            "AGENT_HANDOVER_BROKER_CAPABILITY": "/run/agent-handover/capability",
            "AGENT_PROJECT_ID": "agent-container",
        }
        cases = (
            ({}, VALID_BODY),
            ({key: value for key, value in required.items() if key != "AGENT_HANDOVER_BROKER_SOCKET"}, VALID_BODY),
            ({key: value for key, value in required.items() if key != "AGENT_HANDOVER_BROKER_CAPABILITY"}, VALID_BODY),
            ({key: value for key, value in required.items() if key != "AGENT_PROJECT_ID"}, VALID_BODY),
            (required, b"private-stdin-marker" + b"x" * MAX_REQUEST_BYTES),
        )
        for environment, body in cases:
            with self.subTest(environment=environment, size=len(body)):
                stdout, stderr = StringIO(), StringIO()
                result = run(
                    ["create", "--title", "private-title-marker"],
                    environment,
                    BytesIO(body),
                    stdout,
                    stderr,
                    client_factory=UnexpectedClient,
                )
                self.assertEqual(result, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "error: handover broker request failed\n")
                self.assertNotIn("private-stdin-marker", stderr.getvalue())
                self.assertNotIn("private-title-marker", stderr.getvalue())

    def test_cli_success_prints_path_and_denial_prints_only_fixed_error(self) -> None:
        environment = {
            "AGENT_HANDOVER_BROKER_SOCKET": "/run/agent-handover/broker.sock",
            "AGENT_HANDOVER_BROKER_CAPABILITY": "/run/agent-handover/capability",
            "AGENT_PROJECT_ID": "agent-container",
        }
        calls: list[tuple[str, bytes, dict[str, object]]] = []

        class SuccessfulClient:
            def __init__(self, **options: object) -> None:
                self.options = options

            def create(self, title: str, body: bytes) -> str:
                calls.append((title, body, self.options))
                return "/handovers/agent-container/2026-08-27_123456_abcdef12.md"

        stdout, stderr = StringIO(), StringIO()
        result = run(
            ["create", "--title", "Safe title"],
            environment,
            BytesIO(VALID_BODY),
            stdout,
            stderr,
            client_factory=SuccessfulClient,
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            stdout.getvalue(),
            "/handovers/agent-container/2026-08-27_123456_abcdef12.md\n",
        )
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(calls[0][0:2], ("Safe title", VALID_BODY))
        self.assertEqual(calls[0][2]["project_id"], "agent-container")

        class DeniedClient:
            def __init__(self, **_: object) -> None:
                pass

            def create(self, title: str, body: bytes) -> str:
                raise RuntimeError("private-denial-marker")

        stdout, stderr = StringIO(), StringIO()
        result = run(
            ["create", "--title", "private-title-marker"],
            environment,
            BytesIO(b"private-stdin-marker"),
            stdout,
            stderr,
            client_factory=DeniedClient,
        )
        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "error: handover broker request failed\n")


if __name__ == "__main__":
    unittest.main()
