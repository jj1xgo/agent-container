from io import BytesIO
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from agent_container.github_broker import BrokerSession
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_broker_protocol import BrokerRequest
from agent_container.github_broker_protocol import BrokerResponse
from agent_container.github_broker_protocol import decode_request_frame
from agent_container.github_broker_protocol import encode_request_frame
from agent_container.github_broker_protocol import encode_response_frame
from agent_container.github_broker_protocol import iter_chunk_stream
from agent_container.github_broker_protocol import write_chunk_stream
from agent_container.github_broker_transport import BrokerUploadPackClient
from agent_container.github_broker_transport import handle_upload_pack_connection
from agent_container.github_broker_transport import read_broker_capability
from agent_container.state import Repository


class Duplex:
    def __init__(self, incoming: bytes = b"") -> None:
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
    def __init__(self, stream: Duplex) -> None:
        self.stream = stream
        self.timeout = None
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


class FakeGitHubTransport:
    def __init__(self) -> None:
        self.requests: list[bytes] = []

    def discover(self) -> bytes:
        return b"000eversion 2\n0000"

    def rpc(self, request: bytes):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return (b"0008NAK\n", b"0002")


def chunks(*bodies: bytes) -> bytes:
    stream = BytesIO()
    write_chunk_stream(stream, bodies)
    return stream.getvalue()


class BrokerUploadPackClientTest(unittest.TestCase):
    @mock.patch("agent_container.github_broker_transport.read_broker_capability")
    @mock.patch("agent_container.github_broker_transport.validate_broker_socket")
    def test_authenticates_once_then_exchanges_discovery_and_rpc(
        self, validate_socket: mock.Mock, read_capability: mock.Mock
    ) -> None:
        validate_socket.side_effect = lambda path: path
        read_capability.return_value = "c" * 43
        incoming = (
            encode_response_frame(BrokerResponse(1, "ok"))
            + chunks(b"000eversion 2\n0000")
            + chunks(b"0008NAK\n", b"0002")
        )
        stream = Duplex(incoming)
        fake_socket = FakeSocket(stream)
        client = BrokerUploadPackClient(
            Path("/run/broker.sock"),
            Path("/run/capability"),
            "agent-container",
            "jj1xgo/agent-container",
            socket_factory=lambda *_: fake_socket,
        )

        self.assertEqual(client.discover(), b"000eversion 2\n0000")
        self.assertEqual(list(client.rpc(b"0009done\n0000")), [b"0008NAK\n", b"0002"])

        outgoing = stream.outgoing.getvalue()
        request, consumed = decode_request_frame(outgoing)
        self.assertEqual(request.operation, "git-upload-pack")
        self.assertEqual(request.payload, {"repository": "jj1xgo/agent-container"})
        self.assertEqual(request.capability, "c" * 43)
        rpc = b"".join(
            iter_chunk_stream(BytesIO(outgoing[consumed:]), maximum_total=1024)
        )
        self.assertEqual(rpc, b"0009done\n0000")
        self.assertEqual(fake_socket.connected, "/run/broker.sock")
        client.close()
        self.assertTrue(fake_socket.closed)
        self.assertTrue(stream.closed)

    @mock.patch("agent_container.github_broker_transport.read_broker_capability")
    @mock.patch("agent_container.github_broker_transport.validate_broker_socket")
    def test_denied_response_is_generic_and_closes_socket(
        self, validate_socket: mock.Mock, read_capability: mock.Mock
    ) -> None:
        validate_socket.side_effect = lambda path: path
        read_capability.return_value = "secret-capability-marker"
        stream = Duplex(encode_response_frame(BrokerResponse(1, "denied")))
        fake_socket = FakeSocket(stream)
        client = BrokerUploadPackClient(
            Path("/run/broker.sock"),
            Path("/run/capability"),
            "agent-container",
            "jj1xgo/agent-container",
            socket_factory=lambda *_: fake_socket,
        )
        with self.assertRaises(RuntimeError) as raised:
            client.discover()
        self.assertNotIn("secret-capability-marker", str(raised.exception))
        self.assertTrue(fake_socket.closed)


class BrokerUploadPackServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "state"
        self.root.mkdir(mode=0o700)
        policy = BrokerPolicy.create(
            project_id="agent-container",
            repository="jj1xgo/agent-container",
            default_branch="main",
            protected_branches=("main",),
        )
        self.session = BrokerSession.create(self.root, policy)

    def tearDown(self) -> None:
        self.session.close()
        self.temporary.cleanup()

    def request(self, **changes: object) -> bytes:
        baseline = {
            "version": 1,
            "capability": self.session._capability,
            "project_id": "agent-container",
            "sequence": 1,
            "operation": "git-upload-pack",
            "payload": {"repository": "jj1xgo/agent-container"},
        }
        return encode_request_frame(BrokerRequest(**(baseline | changes)))  # type: ignore[arg-type]

    def test_authorizes_and_bridges_upload_pack_without_token(self) -> None:
        rpc = b"0009done\n0000"
        connection = Duplex(self.request() + chunks(rpc) + chunks())
        transport = FakeGitHubTransport()

        self.assertEqual(
            handle_upload_pack_connection(self.session, connection, transport), 0
        )
        self.assertEqual(transport.requests, [rpc])
        output = connection.outgoing.getvalue()
        response_size = int.from_bytes(output[:4], "big") + 4
        self.assertEqual(output[4:response_size], b'{"status":"ok","version":1}')
        discovery = b"".join(
            iter_chunk_stream(
                BytesIO(output[response_size:]), maximum_total=1024
            )
        )
        self.assertEqual(discovery, transport.discover())
        self.assertNotIn(b"token", output.lower())

    def test_rejects_wrong_repository_before_transport(self) -> None:
        marker = "secret-marker"
        connection = Duplex(
            self.request(payload={"repository": f"jj1xgo/{marker}"})
        )
        transport = FakeGitHubTransport()
        self.assertEqual(
            handle_upload_pack_connection(self.session, connection, transport), 1
        )
        self.assertEqual(transport.requests, [])
        self.assertNotIn(marker.encode(), connection.outgoing.getvalue())
        self.assertIn(b'"status":"denied"', connection.outgoing.getvalue())


class BrokerRuntimePathTest(unittest.TestCase):
    def test_reads_exact_private_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capability"
            path.write_text("c" * 43 + "\n", encoding="ascii")
            path.chmod(0o600)
            self.assertEqual(read_broker_capability(path), "c" * 43)

    def test_rejects_broad_or_symlinked_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("c" * 43 + "\n", encoding="ascii")
            target.chmod(0o600)
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                read_broker_capability(link)
            target.chmod(0o644)
            with self.assertRaises(ValueError):
                read_broker_capability(target)
