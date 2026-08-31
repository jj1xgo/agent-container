from io import BytesIO, StringIO
from pathlib import Path
import socket
import unittest
from unittest import mock

from agent_container.family_intake_client import connect_family_intake
from agent_container.family_intake_client import run
from agent_container.family_intake_client import run_create
from agent_container.family_intake_protocol import decode_request_frame
from agent_container.family_intake_protocol import encode_response_frame
from agent_container.family_intake_protocol import FamilyIntakeResponse


class Duplex:
    def __init__(self, incoming: bytes, *, write_limit: int | None = None) -> None:
        self.incoming = BytesIO(incoming)
        self.outgoing = BytesIO()
        self.write_limit = write_limit
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self.incoming.read(size)

    def write(self, body: bytes) -> int:
        limit = self.write_limit or len(body)
        return self.outgoing.write(body[:limit])

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FailingCloseDuplex(Duplex):
    def close(self) -> None:
        self.closed = True
        raise OSError("stream close failed")


class FakeSocket:
    def __init__(self, stream: Duplex, *, error: OSError | None = None) -> None:
        self.stream = stream
        self.error = error
        self.family: int | None = None
        self.kind: int | None = None
        self.connected: str | None = None
        self.closed = False

    def settimeout(self, _: float) -> None:
        pass

    def connect(self, path: str) -> None:
        if self.error is not None:
            raise self.error
        self.connected = path

    def makefile(self, *_: object, **__: object) -> Duplex:
        return self.stream

    def close(self) -> None:
        self.closed = True


class FamilyIntakeClientTest(unittest.TestCase):
    def test_create_validates_draft_before_connecting_and_sends_only_one_request(self) -> None:
        environment = {
            "AGENT_FAMILY_SOCKET": "/run/agent-family/intake.sock",
            "AGENT_FAMILY_CAPABILITY": "private-capability-marker",
        }
        stream = Duplex(
            encode_response_frame(
                FamilyIntakeResponse(1, "pending", "request-123", 1_800_086_400)
            ),
            write_limit=3,
        )
        fake_socket = FakeSocket(stream)

        response = run_create(
            [
                "issue", "create", "--title", "Add export", "--summary", "Portable copy",
                "--context", "There is no export action", "--acceptance-criterion", "A JSON file downloads",
            ],
            environment=environment,
            connector=lambda request, path: connect_family_intake(
                request, path, socket_factory=lambda family, kind: fake_socket
            ),
        )

        captured, consumed = decode_request_frame(stream.outgoing.getvalue())
        self.assertEqual(response.status, "pending")
        self.assertEqual(captured.operation, "issue_create_request")
        self.assertEqual(captured.capability, environment["AGENT_FAMILY_CAPABILITY"])
        self.assertEqual(captured.payload["title"], "Add export")
        self.assertEqual(consumed, len(stream.outgoing.getvalue()))
        self.assertEqual(fake_socket.connected, environment["AGENT_FAMILY_SOCKET"])
        self.assertTrue(stream.closed)
        self.assertTrue(fake_socket.closed)

    def test_create_rejects_invalid_draft_without_connecting(self) -> None:
        socket_factory = mock.Mock()

        with self.assertRaises(ValueError):
            run_create(
                [
                    "issue", "create", "--title", "Bad\ntitle", "--summary", "summary",
                    "--context", "context", "--acceptance-criterion", "criterion",
                ],
                environment={
                    "AGENT_FAMILY_SOCKET": "/run/agent-family/intake.sock",
                    "AGENT_FAMILY_CAPABILITY": "capability",
                },
                connector=lambda request, path: connect_family_intake(
                    request, path, socket_factory=socket_factory
                ),
            )

        socket_factory.assert_not_called()

    def test_create_rejects_missing_capability_before_invoking_connector(self) -> None:
        connector = mock.Mock()

        with self.assertRaises(ValueError):
            run_create(
                [
                    "issue", "create", "--title", "title", "--summary", "summary",
                    "--context", "context", "--acceptance-criterion", "criterion",
                ],
                environment={"AGENT_FAMILY_SOCKET": "/run/agent-family/intake.sock"},
                connector=connector,
            )

        connector.assert_not_called()

    def test_connector_uses_only_unix_stream_socket_and_rejects_trailing_response(self) -> None:
        stream = Duplex(
            encode_response_frame(FamilyIntakeResponse(1, "pending", "request-123", 1))
            + b"trailing"
        )
        fake_socket = FakeSocket(stream)

        calls: list[tuple[int, int]] = []

        def factory(family: int, kind: int) -> FakeSocket:
            calls.append((family, kind))
            return fake_socket

        with self.assertRaises(ValueError):
            connect_family_intake(
                self._request(),
                Path("/run/agent-family/intake.sock"),
                socket_factory=factory,
            )

        self.assertEqual(calls, [(socket.AF_UNIX, socket.SOCK_STREAM)])
        self.assertEqual(fake_socket.connected, "/run/agent-family/intake.sock")
        self.assertTrue(stream.closed)
        self.assertTrue(fake_socket.closed)

    def test_connector_rejects_relative_path_and_socket_errors_without_leaking_request(self) -> None:
        with self.assertRaises(ValueError):
            connect_family_intake(self._request(), Path("relative.sock"))

        fake_socket = FakeSocket(Duplex(b""), error=OSError("private socket path"))
        with self.assertRaises(OSError) as raised:
            connect_family_intake(
                self._request(),
                Path("/run/agent-family/intake.sock"),
                socket_factory=lambda family, kind: fake_socket,
            )
        self.assertNotIn("private-capability-marker", str(raised.exception))
        self.assertTrue(fake_socket.closed)

    def test_create_rejects_noncanonical_raw_socket_environment_without_connecting(self) -> None:
        arguments = [
            "issue", "create", "--title", "title", "--summary", "summary",
            "--context", "context", "--acceptance-criterion", "criterion",
        ]
        for socket_value in (
            "relative.sock",
            "/run//agent-family/intake.sock",
            "/run/./agent-family/intake.sock",
            "//run/agent-family/intake.sock",
            "/run/agent-family/intake.sock/",
            "/run/agent-family/../intake.sock",
            "/run/agent-family/\x00intake.sock",
            "/run/agent-family/\nintake.sock",
            "/run/agent-family/\x85intake.sock",
        ):
            with self.subTest(socket_value=repr(socket_value)):
                connector = mock.Mock()
                with self.assertRaises(ValueError):
                    run_create(
                        arguments,
                        environment={
                            "AGENT_FAMILY_SOCKET": socket_value,
                            "AGENT_FAMILY_CAPABILITY": "capability",
                        },
                        connector=connector,
                    )
                connector.assert_not_called()

    def test_connector_closes_socket_when_stream_close_raises(self) -> None:
        stream = FailingCloseDuplex(
            encode_response_frame(FamilyIntakeResponse(1, "pending", "request-123", 1))
        )
        fake_socket = FakeSocket(stream)

        with self.assertRaisesRegex(OSError, "stream close failed"):
            connect_family_intake(
                self._request(),
                Path("/run/agent-family/intake.sock"),
                socket_factory=lambda family, kind: fake_socket,
            )

        self.assertTrue(stream.closed)
        self.assertTrue(fake_socket.closed)

    def test_cli_emits_only_pending_receipt_and_fixed_errors_without_private_values(self) -> None:
        environment = {
            "AGENT_FAMILY_SOCKET": "/run/private/intake.sock",
            "AGENT_FAMILY_CAPABILITY": "private-capability-marker",
        }
        arguments = [
            "issue", "create", "--title", "private title", "--summary", "private summary",
            "--context", "private context", "--acceptance-criterion", "private criterion",
        ]
        stdout, stderr = StringIO(), StringIO()

        result = run(
            arguments,
            environment,
            stdout,
            stderr,
            connector=lambda *_: FamilyIntakeResponse(1, "pending", "request-123", 1_800_086_400),
        )

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue(), "pending request-123 1800086400\n")
        self.assertEqual(stderr.getvalue(), "")
        for private_value in (*environment.values(), "private title", "private summary", "private context", "private criterion", "repository", "/host/path"):
            self.assertNotIn(private_value, stdout.getvalue())

        stdout, stderr = StringIO(), StringIO()
        result = run(arguments, environment, stdout, stderr, connector=lambda *_: (_ for _ in ()).throw(OSError("private failure")))
        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "error: family intake request failed\n")
        for private_value in (*environment.values(), "private title", "private summary", "private context", "private criterion", "private failure"):
            self.assertNotIn(private_value, stderr.getvalue())

    @staticmethod
    def _request() -> object:
        from agent_container.family_intake_protocol import FamilyIntakeRequest

        return FamilyIntakeRequest(
            1,
            "issue_create_request",
            "private-capability-marker",
            {
                "title": "Title",
                "summary": "Summary",
                "context": "Context",
                "acceptance_criteria": ["Criterion"],
            },
        )
