from io import BytesIO
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import agent_container.github_broker_transport as broker_transport
from agent_container.github_broker import BrokerSession
from agent_container.github_broker_error import BrokerStageError
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_broker_protocol import BrokerRequest
from agent_container.github_broker_protocol import BrokerResponse
from agent_container.github_broker_protocol import decode_request_frame
from agent_container.github_broker_protocol import encode_request_frame
from agent_container.github_broker_protocol import encode_response_frame
from agent_container.github_broker_protocol import iter_chunk_stream
from agent_container.github_broker_protocol import MAX_STREAM_CHUNK_BYTES
from agent_container.github_broker_protocol import write_chunk_stream
from agent_container.github_broker_transport import BrokerUploadPackClient
from agent_container.github_broker_transport import BrokerReceivePackClient
from agent_container.github_broker_transport import handle_broker_connection
from agent_container.github_broker_transport import handle_receive_pack_connection
from agent_container.github_broker_transport import handle_pull_request_connection
from agent_container.github_broker_transport import handle_upload_pack_connection
from agent_container.github_broker_transport import read_broker_capability
from agent_container.github_issue import MAX_ISSUE_RESPONSE_BYTES
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


class FailingWriteDuplex(Duplex):
    def __init__(self, incoming: bytes, successful_writes: int) -> None:
        super().__init__(incoming)
        self.successful_writes = successful_writes
        self.write_calls = 0

    def write(self, body: bytes) -> int:
        if self.write_calls >= self.successful_writes:
            raise OSError("secret-stream-marker")
        self.write_calls += 1
        return super().write(body)


class FailingReadDuplex(Duplex):
    def __init__(self, incoming: bytes, successful_reads: int) -> None:
        super().__init__(incoming)
        self.successful_reads = successful_reads
        self.read_calls = 0

    def read(self, size: int = -1) -> bytes:
        if self.read_calls >= self.successful_reads:
            raise OSError("secret-read-marker")
        self.read_calls += 1
        return super().read(size)


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


class FailingGitHubTransport(FakeGitHubTransport):
    def __init__(self, stage: str) -> None:
        super().__init__()
        self.stage = stage

    def discover(self) -> bytes:
        if self.stage == "upload-discovery":
            raise BrokerStageError(self.stage)
        return super().discover()

    def rpc(self, request: bytes):  # type: ignore[no-untyped-def]
        raise BrokerStageError(self.stage)


class FakeReceivePackTransport:
    def __init__(self, discovery: bytes) -> None:
        self.discovery = discovery
        self.requests: list[bytes] = []

    def discover(self) -> bytes:
        return self.discovery

    def rpc(self, request: bytes):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return (b"000eunpack ok\n", b"0000")


class FailingReceivePackTransport(FakeReceivePackTransport):
    def __init__(self, stage: str) -> None:
        super().__init__(receive_advertisement())
        self.stage = stage

    def discover(self) -> bytes:
        if self.stage == "receive-discovery":
            raise BrokerStageError(self.stage)
        return super().discover()

    def rpc(self, request: bytes):  # type: ignore[no-untyped-def]
        raise BrokerStageError(self.stage)


class FakePullRequestTransport:
    def __init__(self) -> None:
        self.calls = []

    def create(self, **payload):  # type: ignore[no-untyped-def]
        self.calls.append(("create", payload))
        return {"number": 12, "state": "open", "title": payload["title"], "url": "https://github.com/example/pull/12"}

    def view(self, number):  # type: ignore[no-untyped-def]
        self.calls.append(("view", number))
        return {"number": number, "state": "open", "title": "Feature", "url": "https://github.com/example/pull/12"}

    def checks(self, number):  # type: ignore[no-untyped-def]
        self.calls.append(("checks", number))
        return {"number": number, "checks": []}


class FailingPullRequestTransport(FakePullRequestTransport):
    def view(self, number):  # type: ignore[no-untyped-def]
        raise BrokerStageError("pr-request")


class BuggyPullRequestTransport(FakePullRequestTransport):
    def view(self, number):  # type: ignore[no-untyped-def]
        raise TypeError("programming-error-marker")


def issue_summary(number: int) -> dict[str, object]:
    return {
        "number": number,
        "title": "content-title-marker",
        "state": "open",
        "author": "content-author-marker",
        "labels": ["content-label-marker"],
        "created_at": "2026-08-28T00:00:00Z",
        "updated_at": "2026-08-28T01:00:00Z",
        "url": f"https://github.com/jj1xgo/agent-container/issues/{number}",
    }


class FakeIssueTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object | None]] = []

    def list_open(self) -> dict[str, object]:
        self.calls.append(("list_open", None))
        return {"issues": []}

    def view(self, number: int) -> dict[str, object]:
        self.calls.append(("view", number))
        return issue_summary(number) | {"body": "content-body-marker"}


class FailingIssueTransport(FakeIssueTransport):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def view(self, number: int) -> dict[str, object]:
        self.calls.append(("view", number))
        if not self.failed:
            self.failed = True
            raise BrokerStageError("issue-request")
        return issue_summary(number) | {"body": "content-body-marker"}


class OversizeIssueTransport(FakeIssueTransport):
    def list_open(self) -> dict[str, object]:
        self.calls.append(("list_open", None))
        return {"issues": ["x" * MAX_ISSUE_RESPONSE_BYTES]}


class LargeIssueTransport(FakeIssueTransport):
    def __init__(self) -> None:
        super().__init__()
        self.response = {
            "issues": [
                issue_summary(12)
                | {"author": "a" * MAX_STREAM_CHUNK_BYTES}
            ]
        }

    def list_open(self) -> dict[str, object]:
        self.calls.append(("list_open", None))
        return self.response


def pkt(payload: bytes) -> bytes:
    return f"{len(payload) + 4:04x}".encode("ascii") + payload


OLD = "1" * 40
NEW = "2" * 40


def receive_advertisement() -> bytes:
    return (
        pkt(
            f"{OLD} refs/heads/feat/work".encode()
            + b"\0report-status side-band-64k object-format=sha1\n"
        )
        + b"0000"
    )


def receive_request(ref: str = "refs/heads/feat/work", old: str = OLD) -> bytes:
    return (
        pkt(
            f"{old} {NEW} {ref}".encode()
            + b"\0report-status side-band-64k object-format=sha1\n"
        )
        + b"0000PACKpayload"
    )


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

    @mock.patch("agent_container.github_broker_transport.read_broker_capability")
    @mock.patch("agent_container.github_broker_transport.validate_broker_socket")
    def test_receive_client_uses_receive_operation_and_push_stream(
        self, validate_socket: mock.Mock, read_capability: mock.Mock
    ) -> None:
        validate_socket.side_effect = lambda path: path
        read_capability.return_value = "c" * 43
        incoming = (
            encode_response_frame(BrokerResponse(1, "ok"))
            + chunks(receive_advertisement())
            + chunks(b"push-result")
        )
        stream = Duplex(incoming)
        client = BrokerReceivePackClient(
            Path("/run/broker.sock"),
            Path("/run/capability"),
            "agent-container",
            "jj1xgo/agent-container",
            socket_factory=lambda *_: FakeSocket(stream),
        )

        self.assertEqual(client.discover(), receive_advertisement())
        self.assertEqual(list(client.push(receive_request())), [b"push-result"])
        request, consumed = decode_request_frame(stream.outgoing.getvalue())
        self.assertEqual(request.operation, "git-receive-pack")
        pushed = b"".join(
            iter_chunk_stream(
                BytesIO(stream.outgoing.getvalue()[consumed:]), maximum_total=4096
            )
        )
        self.assertEqual(pushed, receive_request())
        client.close()
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

    def test_audits_upload_stage_failures_without_stopping_handler(self) -> None:
        cases = (
            (
                "upload-discovery",
                Duplex(self.request(sequence=20)),
            ),
            (
                "upload-rpc",
                Duplex(
                    self.request(sequence=21)
                    + chunks(b"0009done\n0000")
                ),
            ),
        )
        for stage, connection in cases:
            with self.subTest(stage=stage):
                result = handle_upload_pack_connection(
                    self.session,
                    connection,
                    FailingGitHubTransport(stage),
                )
                self.assertEqual(result, 1)
                self.assertNotIn(b"secret", connection.outgoing.getvalue())

        records = [
            json.loads(line)
            for line in self.session.audit_file.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [(record["status"], record["stage"]) for record in records],
            [
                ("error", "upload-discovery"),
                ("error", "upload-rpc"),
            ],
        )

    def test_receive_pack_gates_then_forwards_work_branch(self) -> None:
        connection = Duplex(
            self.request(operation="git-receive-pack") + chunks(receive_request())
        )
        transport = FakeReceivePackTransport(receive_advertisement())

        self.assertEqual(
            handle_receive_pack_connection(self.session, connection, transport), 0
        )
        self.assertEqual(transport.requests, [receive_request()])
        self.assertIn(b'"status":"ok"', connection.outgoing.getvalue())

    def test_receive_pack_rejects_protected_ref_before_rpc(self) -> None:
        connection = Duplex(
            self.request(operation="git-receive-pack")
            + chunks(receive_request(ref="refs/heads/main", old="0" * 40))
        )
        transport = FakeReceivePackTransport(receive_advertisement())

        self.assertEqual(
            handle_receive_pack_connection(self.session, connection, transport), 1
        )
        self.assertEqual(transport.requests, [])

    def test_audits_receive_stage_failures(self) -> None:
        cases = (
            (
                "receive-discovery",
                Duplex(self.request(sequence=30, operation="git-receive-pack")),
            ),
            (
                "receive-rpc",
                Duplex(
                    self.request(sequence=31, operation="git-receive-pack")
                    + chunks(receive_request())
                ),
            ),
        )
        for stage, connection in cases:
            with self.subTest(stage=stage):
                result = handle_receive_pack_connection(
                    self.session,
                    connection,
                    FailingReceivePackTransport(stage),
                )
                self.assertEqual(result, 1)

        records = [
            json.loads(line)
            for line in self.session.audit_file.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [(record["status"], record["stage"]) for record in records],
            [
                ("error", "receive-discovery"),
                ("error", "receive-rpc"),
            ],
        )

    def test_audits_authorized_stream_failure(self) -> None:
        connection = Duplex(
            self.request(sequence=32)
            + b"\x00\x00\x00\x01"
        )

        result = handle_upload_pack_connection(
            self.session,
            connection,
            FakeGitHubTransport(),
        )

        self.assertEqual(result, 1)
        record = json.loads(
            self.session.audit_file.read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["stage"], "response-stream")

    def test_audits_authorized_write_failure_without_stopping_handler(self) -> None:
        connection = FailingWriteDuplex(
            self.request(sequence=33),
            successful_writes=1,
        )

        result = handle_upload_pack_connection(
            self.session,
            connection,
            FakeGitHubTransport(),
        )

        self.assertEqual(result, 1)
        record = json.loads(
            self.session.audit_file.read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["stage"], "response-stream")
        self.assertNotIn(
            "secret-stream-marker",
            self.session.audit_file.read_text(encoding="utf-8"),
        )

    def test_audits_authorized_read_failure_without_stopping_handler(self) -> None:
        connection = FailingReadDuplex(
            self.request(sequence=34),
            successful_reads=2,
        )

        result = handle_upload_pack_connection(
            self.session,
            connection,
            FakeGitHubTransport(),
        )

        self.assertEqual(result, 1)
        audit = self.session.audit_file.read_text(encoding="utf-8")
        record = json.loads(audit.splitlines()[-1])
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["stage"], "response-stream")
        self.assertNotIn("secret-read-marker", audit)

    def test_pull_request_operations_return_bounded_json_and_audit(self) -> None:
        transport = FakePullRequestTransport()
        for sequence, operation, payload in (
            (10, "pr-create", {"base": "main", "head": "feat/work", "title": "Feature", "body": "Body"}),
            (11, "pr-view", {"number": 12}),
            (12, "pr-checks", {"number": 12}),
        ):
            with self.subTest(operation=operation):
                connection = Duplex(self.request(sequence=sequence, operation=operation, payload=payload))
                self.assertEqual(handle_pull_request_connection(self.session, connection, transport), 0)
                output = connection.outgoing.getvalue()
                response_size = int.from_bytes(output[:4], "big") + 4
                self.assertIn(b'"status":"ok"', output[:response_size])
                result = json.loads(b"".join(iter_chunk_stream(BytesIO(output[response_size:]), maximum_total=4096)))
                self.assertEqual(result["number"], 12)
        audit = self.session.audit_file.read_text(encoding="utf-8")
        self.assertIn('"operation":"pr-create"', audit)
        self.assertIn('"pr_number":12', audit)
        self.assertNotIn("Body", audit)

    def test_pull_request_rejects_unknown_fields_without_leaking_body(self) -> None:
        marker = "secret-body-marker"
        connection = Duplex(self.request(operation="pr-create", payload={"base": "main", "head": "feat/work", "title": "Feature", "body": marker, "repository": "other/repo"}))
        transport = FakePullRequestTransport()
        self.assertEqual(handle_pull_request_connection(self.session, connection, transport), 1)
        self.assertEqual(transport.calls, [])
        self.assertIn(b'"status":"denied"', connection.outgoing.getvalue())
        self.assertNotIn(marker.encode(), connection.outgoing.getvalue())

    def test_audits_pull_request_stage_failure(self) -> None:
        connection = Duplex(
            self.request(
                sequence=40,
                operation="pr-view",
                payload={"number": 12},
            )
        )

        result = handle_pull_request_connection(
            self.session,
            connection,
            FailingPullRequestTransport(),
        )

        self.assertEqual(result, 1)
        record = json.loads(
            self.session.audit_file.read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["stage"], "pr-request")

    def test_pull_request_programming_error_surfaces(self) -> None:
        connection = Duplex(
            self.request(
                sequence=41,
                operation="pr-view",
                payload={"number": 12},
            )
        )

        with self.assertRaisesRegex(TypeError, "programming-error-marker"):
            handle_pull_request_connection(
                self.session,
                connection,
                BuggyPullRequestTransport(),
            )

    def test_issue_operations_dispatch_exact_payloads_and_audit_metadata(self) -> None:
        transport = FakeIssueTransport()
        accepted = (
            ("issue-list", {}, {"issues": []}),
            (
                "issue-view",
                {"number": 12},
                issue_summary(12) | {"body": "content-body-marker"},
            ),
        )

        for sequence, (operation, payload, expected) in enumerate(
            accepted, start=50
        ):
            with self.subTest(operation=operation):
                connection = Duplex(
                    self.request(
                        sequence=sequence,
                        operation=operation,
                        payload=payload,
                    )
                )

                result = handle_broker_connection(
                    self.session,
                    connection,
                    FakeGitHubTransport(),
                    None,
                    issue_transport=transport,
                )

                self.assertEqual(result, 0)
                output = connection.outgoing.getvalue()
                response_size = int.from_bytes(output[:4], "big") + 4
                self.assertEqual(
                    output[4:response_size], b'{"status":"ok","version":1}'
                )
                body = b"".join(
                    iter_chunk_stream(
                        BytesIO(output[response_size:]), maximum_total=4096
                    )
                )
                self.assertEqual(json.loads(body), expected)

        self.assertEqual(
            transport.calls,
            [("list_open", None), ("view", 12)],
        )
        records = [
            json.loads(line)
            for line in self.session.audit_file.read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertNotIn("issue_number", records[-2])
        self.assertEqual(records[-1]["issue_number"], 12)
        audit = self.session.audit_file.read_text(encoding="utf-8")
        for marker in (
            "content-title-marker",
            "content-author-marker",
            "content-label-marker",
            "content-body-marker",
        ):
            self.assertNotIn(marker, audit)

    def test_issue_operations_reject_non_exact_payloads_before_transport(self) -> None:
        denied = (
            ("issue-list", {"page": 2}),
            ("issue-view", {"number": True}),
            (
                "issue-view",
                {"number": 12, "repository": "other/repo"},
            ),
            ("issue-comment", {"number": 12, "body": "x"}),
        )
        transport = FakeIssueTransport()

        for sequence, (operation, payload) in enumerate(denied, start=60):
            with self.subTest(operation=operation, payload=payload):
                connection = Duplex(
                    self.request(
                        sequence=sequence,
                        operation=operation,
                        payload=payload,
                    )
                )
                result = handle_broker_connection(
                    self.session,
                    connection,
                    FakeGitHubTransport(),
                    None,
                    issue_transport=transport,
                )
                self.assertEqual(result, 1)
                self.assertIn(
                    b'"status":"denied"', connection.outgoing.getvalue()
                )

        self.assertEqual(transport.calls, [])

    def test_issue_stage_error_is_fixed_and_does_not_break_later_connection(self) -> None:
        transport = FailingIssueTransport()
        failed = Duplex(
            self.request(
                sequence=70,
                operation="issue-view",
                payload={"number": 12},
            )
        )

        self.assertEqual(
            broker_transport.handle_issue_connection(
                self.session, failed, transport
            ),
            1,
        )
        self.assertEqual(
            failed.outgoing.getvalue(),
            encode_response_frame(BrokerResponse(1, "error")),
        )
        record = json.loads(
            self.session.audit_file.read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(record["stage"], "issue-request")
        self.assertEqual(record["issue_number"], 12)

        later = Duplex(
            self.request(
                sequence=71,
                operation="issue-view",
                payload={"number": 12},
            )
        )
        self.assertEqual(
            broker_transport.handle_issue_connection(
                self.session, later, transport
            ),
            0,
        )
        self.assertIn(b'"status":"ok"', later.outgoing.getvalue())

    def test_issue_rejects_oversize_response_before_ok(self) -> None:
        connection = Duplex(
            self.request(
                sequence=72,
                operation="issue-list",
                payload={},
            )
        )

        result = broker_transport.handle_issue_connection(
            self.session, connection, OversizeIssueTransport()
        )

        self.assertEqual(result, 1)
        self.assertNotIn(b'"status":"ok"', connection.outgoing.getvalue())
        self.assertEqual(
            connection.outgoing.getvalue(),
            encode_response_frame(BrokerResponse(1, "error")),
        )
        record = json.loads(
            self.session.audit_file.read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(record["stage"], "issue-request")

    def test_issue_streams_valid_response_larger_than_one_chunk(self) -> None:
        transport = LargeIssueTransport()
        connection = Duplex(
            self.request(
                sequence=74,
                operation="issue-list",
                payload={},
            )
        )

        result = broker_transport.handle_issue_connection(
            self.session, connection, transport
        )

        self.assertEqual(result, 0)
        output = connection.outgoing.getvalue()
        response_size = int.from_bytes(output[:4], "big") + 4
        response_chunks = list(
            iter_chunk_stream(
                BytesIO(output[response_size:]),
                maximum_total=MAX_ISSUE_RESPONSE_BYTES,
            )
        )
        self.assertEqual(len(response_chunks), 2)
        self.assertEqual(len(response_chunks[0]), MAX_STREAM_CHUNK_BYTES)
        self.assertGreater(len(response_chunks[1]), 0)
        self.assertLessEqual(
            len(response_chunks[1]), MAX_STREAM_CHUNK_BYTES
        )
        self.assertEqual(
            json.loads(b"".join(response_chunks)), transport.response
        )
        record = json.loads(
            self.session.audit_file.read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["bytes"], sum(map(len, response_chunks)))

    def test_issue_audits_response_stream_failure(self) -> None:
        connection = FailingWriteDuplex(
            self.request(
                sequence=73,
                operation="issue-list",
                payload={},
            ),
            successful_writes=1,
        )

        result = broker_transport.handle_issue_connection(
            self.session, connection, FakeIssueTransport()
        )

        self.assertEqual(result, 1)
        record = json.loads(
            self.session.audit_file.read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["stage"], "response-stream")


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
