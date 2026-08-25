from io import BytesIO, StringIO
import json
from pathlib import Path
import unittest
from unittest import mock

from agent_container.github_broker_protocol import BrokerResponse
from agent_container.github_broker_protocol import decode_request_frame
from agent_container.github_broker_protocol import encode_response_frame
from agent_container.github_broker_protocol import write_chunk_stream
from agent_container.github_client import request_pull_request
from agent_container.github_client import run


class Duplex:
    def __init__(self, incoming: bytes) -> None:
        self.incoming = BytesIO(incoming)
        self.outgoing = BytesIO()

    def read(self, size: int = -1) -> bytes:
        return self.incoming.read(size)

    def write(self, body: bytes) -> int:
        return self.outgoing.write(body)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class FakeSocket:
    def __init__(self, stream: Duplex) -> None:
        self.stream = stream
        self.connected = None
        self.closed = False

    def settimeout(self, timeout: int) -> None:
        self.timeout = timeout

    def connect(self, path: str) -> None:
        self.connected = path

    def makefile(self, *_: object, **__: object) -> Duplex:
        return self.stream

    def close(self) -> None:
        self.closed = True


def response(result: dict[str, object]) -> bytes:
    stream = BytesIO()
    stream.write(encode_response_frame(BrokerResponse(1, "ok")))
    write_chunk_stream(stream, (json.dumps(result).encode(),))
    return stream.getvalue()


class GitHubClientTest(unittest.TestCase):
    def test_cli_exposes_only_fixed_pull_request_operations(self) -> None:
        calls = []

        def requester(operation, payload, environment):  # type: ignore[no-untyped-def]
            calls.append((operation, payload, environment))
            return {"number": 12, "state": "open"}

        stdout, stderr = StringIO(), StringIO()
        status = run(
            [
                "pr", "create", "--base", "main", "--head", "feat/work",
                "--title", "Feature", "--body", "Body",
            ],
            {"AGENT_PROJECT_ID": "agent-container"},
            stdout,
            stderr,
            requester=requester,
        )
        self.assertEqual(status, 0)
        self.assertEqual(calls[0][0], "pr-create")
        self.assertEqual(calls[0][1]["head"], "feat/work")
        self.assertEqual(json.loads(stdout.getvalue())["number"], 12)
        self.assertEqual(stderr.getvalue(), "")

        for arguments, operation in ((["pr", "view", "12"], "pr-view"), (["pr", "checks", "12"], "pr-checks")):
            with self.subTest(arguments=arguments):
                self.assertEqual(run(arguments, {}, StringIO(), StringIO(), requester=requester), 0)
                self.assertEqual(calls[-1][0], operation)
                self.assertEqual(calls[-1][1], {"number": 12})

    @mock.patch("agent_container.github_client.read_broker_capability", return_value="c" * 43)
    @mock.patch("agent_container.github_client.validate_broker_socket")
    def test_request_crosses_socket_without_repository_or_token(
        self, validate_socket: mock.Mock, read_capability: mock.Mock
    ) -> None:
        stream = Duplex(response({"number": 12, "state": "open"}))
        client = FakeSocket(stream)
        result = request_pull_request(
            "pr-view",
            {"number": 12},
            {
                "AGENT_BROKER_SOCKET": "/run/broker.sock",
                "AGENT_BROKER_CAPABILITY": "/run/capability",
                "AGENT_PROJECT_ID": "agent-container",
            },
            socket_factory=lambda *_: client,
        )
        request, _ = decode_request_frame(stream.outgoing.getvalue())
        self.assertEqual(result["number"], 12)
        self.assertEqual(request.operation, "pr-view")
        self.assertEqual(request.payload, {"number": 12})
        self.assertNotIn(b"repository", stream.outgoing.getvalue())
        self.assertNotIn(b"token", stream.outgoing.getvalue().lower())
        self.assertEqual(client.connected, "/run/broker.sock")
        self.assertTrue(client.closed)

    @mock.patch("agent_container.github_client.read_broker_capability", return_value="secret-marker")
    @mock.patch("agent_container.github_client.validate_broker_socket")
    def test_denial_is_secret_free(
        self, validate_socket: mock.Mock, read_capability: mock.Mock
    ) -> None:
        client = FakeSocket(Duplex(encode_response_frame(BrokerResponse(1, "denied"))))
        with self.assertRaises(RuntimeError) as raised:
            request_pull_request(
                "pr-view", {"number": 12},
                {"AGENT_BROKER_SOCKET": "/run/broker.sock", "AGENT_BROKER_CAPABILITY": "/run/capability"},
                socket_factory=lambda *_: client,
            )
        self.assertNotIn("secret-marker", str(raised.exception))
