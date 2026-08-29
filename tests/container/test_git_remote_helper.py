from io import BytesIO
import unittest

from agent_container.git_remote_helper import MAX_STATELESS_REQUEST_BYTES
from agent_container.git_remote_helper import StatelessRemoteHelper
from agent_container.git_remote_helper import parse_broker_repository_url
from agent_container.git_remote_helper import read_stateless_request
from agent_container.git_remote_helper import run_remote_helper
from agent_container.state import Repository


def pkt(body: bytes) -> bytes:
    return f"{len(body) + 4:04x}".encode() + body


class FakeTransport:
    def __init__(self) -> None:
        self.requests: list[bytes] = []

    def discover(self) -> bytes:
        return pkt(b"version 2\n") + pkt(b"ls-refs\n") + b"0000"

    def rpc(self, request: bytes):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return (pkt(b"response\n"), b"0002")


class FakeReceiveTransport:
    def __init__(self) -> None:
        self.requests: list[bytes] = []

    def discover(self) -> bytes:
        return b"advertisement"

    def push(self, request: bytes):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return (b"push-result",)


class StatelessRequestTest(unittest.TestCase):
    def test_reads_one_request_including_delimiter_and_flush(self) -> None:
        first = pkt(b"command=fetch\n") + b"0001" + pkt(b"done\n") + b"0000"
        stream = BytesIO(first + pkt(b"next\n") + b"0000")
        self.assertEqual(read_stateless_request(stream), first)
        self.assertEqual(read_stateless_request(stream), pkt(b"next\n") + b"0000")
        self.assertIsNone(read_stateless_request(stream))

    def test_rejects_invalid_incomplete_and_oversized_requests(self) -> None:
        cases = (
            b"bad!",
            b"0002",
            b"0008abc",
            b"ffff" + b"x" * (0xFFFF - 4),
        )
        for body in cases:
            with self.subTest(body=body[:10]):
                with self.assertRaises(ValueError):
                    read_stateless_request(BytesIO(body))
        oversized = pkt(b"x" * 1000) * (MAX_STATELESS_REQUEST_BYTES // 1004 + 1)
        with self.assertRaisesRegex(ValueError, "too large"):
            read_stateless_request(BytesIO(oversized))


class RemoteHelperTest(unittest.TestCase):
    def test_accepts_only_exact_project_repository_url(self) -> None:
        repository = parse_broker_repository_url(
            "agent-broker://jj1xgo/agent-container", "jj1xgo/agent-container"
        )
        self.assertEqual(repository.slug, "jj1xgo/agent-container")
        for value in (
            "https://github.com/jj1xgo/agent-container",
            "agent-broker://jj1xgo/other",
            "agent-broker://user@jj1xgo/agent-container",
            "agent-broker://jj1xgo/agent-container?token=secret",
            "agent-broker://jj1xgo/a/b",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_broker_repository_url(value, "jj1xgo/agent-container")

    def test_runs_stateless_upload_pack_exchange(self) -> None:
        transport = FakeTransport()
        request = pkt(b"command=ls-refs\n") + b"0000"
        stdin = BytesIO(
            b"capabilities\nstateless-connect git-upload-pack\n" + request
        )
        stdout = BytesIO()

        result = run_remote_helper(
            ["origin", "agent-broker://jj1xgo/agent-container"],
            {"AGENT_BROKER_REPOSITORY": "jj1xgo/agent-container"},
            transport,
            stdin,
            stdout,
        )

        self.assertEqual(result, 0)
        self.assertEqual(transport.requests, [request])
        self.assertEqual(
            stdout.getvalue(),
            b"connect\nstateless-connect\n\n\n"
            + transport.discover()
            + pkt(b"response\n")
            + b"0002",
        )

    def test_runs_connect_upload_pack_exchange_for_git_2_53(self) -> None:
        transport = FakeTransport()
        request = pkt(b"command=ls-refs\n") + b"0000"
        stdin = BytesIO(b"capabilities\nconnect git-upload-pack\n" + request)
        stdout = BytesIO()

        result = run_remote_helper(
            ["origin", "agent-broker://jj1xgo/agent-container"],
            {"AGENT_BROKER_REPOSITORY": "jj1xgo/agent-container"},
            transport,
            stdin,
            stdout,
        )

        self.assertEqual(result, 0)
        self.assertEqual(transport.requests, [request])
        self.assertEqual(
            stdout.getvalue(),
            b"connect\nstateless-connect\n\n\n"
            + transport.discover()
            + pkt(b"response\n")
            + b"0002",
        )

    def test_runs_one_receive_pack_exchange(self) -> None:
        transport = FakeReceiveTransport()
        push = b"commands-and-pack"
        stdin = BytesIO(
            b"capabilities\nconnect git-receive-pack\n" + push
        )
        stdout = BytesIO()

        result = run_remote_helper(
            ["origin", "agent-broker://jj1xgo/agent-container"],
            {"AGENT_BROKER_REPOSITORY": "jj1xgo/agent-container"},
            transport,
            stdin,
            stdout,
        )

        self.assertEqual(result, 0)
        self.assertEqual(transport.requests, [push])
        self.assertEqual(
            stdout.getvalue(),
            b"connect\nstateless-connect\n\n\nadvertisementpush-result",
        )

    def test_stateless_helper_rejects_non_packet_commands(self) -> None:
        repository = Repository.parse("jj1xgo/agent-container")
        for body in (
            b"list\n",
            b"capabilities\nstateless-connect git-receive-pack\n",
        ):
            with self.subTest(body=body):
                helper = StatelessRemoteHelper(
                    repository, FakeTransport(), BytesIO(body), BytesIO()
                )
                with self.assertRaises(ValueError):
                    helper.run()

    def test_run_remote_helper_rejects_unknown_service(self) -> None:
        transport = FakeTransport()

        with self.assertRaisesRegex(ValueError, "service is not allowed"):
            run_remote_helper(
                ["origin", "agent-broker://jj1xgo/agent-container"],
                {"AGENT_BROKER_REPOSITORY": "jj1xgo/agent-container"},
                transport,
                BytesIO(b"capabilities\nconnect git-archive\n"),
                BytesIO(),
            )

        self.assertEqual(transport.requests, [])

    def test_does_not_write_transport_error_or_credential(self) -> None:
        class Failing(FakeTransport):
            def discover(self) -> bytes:
                raise RuntimeError("generic failure")

        output = BytesIO()
        helper = StatelessRemoteHelper(
            Repository.parse("jj1xgo/agent-container"),
            Failing(),
            BytesIO(b"capabilities\nstateless-connect git-upload-pack\n"),
            output,
        )
        with self.assertRaises(RuntimeError):
            helper.run()
        self.assertNotIn(b"token", output.getvalue().lower())
