from io import BytesIO
import unittest

from agent_container.git_remote_helper import MAX_STATELESS_REQUEST_BYTES
from agent_container.git_remote_helper import _is_delete_only_receive_pack
from agent_container.git_remote_helper import StatelessRemoteHelper
from agent_container.git_remote_helper import parse_broker_repository_url
from agent_container.git_remote_helper import read_stateless_request
from agent_container.git_remote_helper import run_remote_helper
from agent_container.state import Repository


def pkt(body: bytes) -> bytes:
    return f"{len(body) + 4:04x}".encode() + body


def delete_command(
    old: bytes,
    ref: bytes,
    *,
    capabilities: bytes | None = None,
) -> bytes:
    body = old + b" " + b"0" * len(old) + b" " + ref
    if capabilities is not None:
        body += b"\0" + capabilities
    return pkt(body + b"\n")


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


class OpenDeleteRequestStream(BytesIO):
    """Model Git waiting for a delete response without closing helper stdin."""

    def read(self, size: int = -1) -> bytes:
        remaining = len(self.getbuffer()) - self.tell()
        if size < 0 or size > remaining:
            raise BlockingIOError("read would wait for client EOF")
        return super().read(size)


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


class ReceivePackCompletionClassifierTest(unittest.TestCase):
    def test_accepts_only_well_formed_all_delete_sections(self) -> None:
        sha1 = b"1" * 40
        sha256 = b"a" * 64
        cases = {
            "sha1": delete_command(
                sha1,
                b"refs/heads/one",
                capabilities=b"report-status side-band-64k",
            )
            + b"0000",
            "sha256": delete_command(
                sha256,
                b"refs/heads/two",
                capabilities=b"report-status object-format=sha256",
            )
            + b"0000",
            "multiple": delete_command(
                sha1, b"refs/heads/one", capabilities=b"report-status"
            )
            + delete_command(sha1, b"refs/heads/two")
            + b"0000",
        }
        for name, commands in cases.items():
            with self.subTest(name=name):
                self.assertTrue(_is_delete_only_receive_pack(commands))

    def test_rejects_mixed_duplicate_and_malformed_sections(self) -> None:
        sha1 = b"1" * 40
        first = delete_command(
            sha1, b"refs/heads/one", capabilities=b"report-status"
        )
        update = pkt(
            sha1
            + b" "
            + b"2" * 40
            + b" refs/heads/two\n"
        )
        cases = {
            "mixed-delete-update": first + update + b"0000",
            "duplicate-ref": first
            + delete_command(sha1, b"refs/heads/one")
            + b"0000",
            "missing-capabilities": delete_command(
                sha1, b"refs/heads/one"
            )
            + b"0000",
            "second-capability-section": first
            + delete_command(
                sha1,
                b"refs/heads/two",
                capabilities=b"report-status",
            )
            + b"0000",
            "second-nul": delete_command(
                sha1,
                b"refs/heads/one",
                capabilities=b"report-status\0agent=git/2.53",
            )
            + b"0000",
            "mixed-object-formats": first
            + delete_command(b"2" * 64, b"refs/heads/two")
            + b"0000",
            "non-hex-old": delete_command(
                b"z" * 40,
                b"refs/heads/one",
                capabilities=b"report-status",
            )
            + b"0000",
            "trailing-data": first + b"0000PACK",
            "missing-flush": first,
            "empty": b"0000",
            "invalid-length": b"0003",
            "truncated-packet": b"0010short",
        }
        for name, commands in cases.items():
            with self.subTest(name=name):
                self.assertFalse(_is_delete_only_receive_pack(commands))


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
        class SplitResponseTransport(FakeTransport):
            def rpc(self, request: bytes):  # type: ignore[no-untyped-def]
                self.requests.append(request)
                return (pkt(b"response\n"), b"00", b"02")

        transport = SplitResponseTransport()
        first = pkt(b"command=ls-refs\n") + b"0000"
        second = pkt(b"command=fetch\n") + b"0000"
        stdin = BytesIO(
            b"capabilities\nconnect git-upload-pack\n"
            + first
            + second
            + b"0000"
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
        self.assertEqual(transport.requests, [first, second])
        self.assertEqual(
            stdout.getvalue(),
            b"connect\nstateless-connect\n\n\n"
            + transport.discover()
            + pkt(b"response\n")
            + b"0000"
            + pkt(b"response\n")
            + b"0000",
        )

    def test_connect_upload_pack_rejects_missing_response_end(self) -> None:
        class MissingResponseEndTransport(FakeTransport):
            def rpc(self, request: bytes):  # type: ignore[no-untyped-def]
                self.requests.append(request)
                return (pkt(b"response\n"), b"0000")

        with self.assertRaisesRegex(ValueError, "response is invalid"):
            run_remote_helper(
                ["origin", "agent-broker://jj1xgo/agent-container"],
                {"AGENT_BROKER_REPOSITORY": "jj1xgo/agent-container"},
                MissingResponseEndTransport(),
                BytesIO(
                    b"capabilities\nconnect git-upload-pack\n"
                    + pkt(b"command=ls-refs\n")
                    + b"0000"
                ),
                BytesIO(),
            )

    def test_runs_one_receive_pack_exchange(self) -> None:
        transport = FakeReceiveTransport()
        push = (
            pkt(
                b"1111111111111111111111111111111111111111 "
                b"2222222222222222222222222222222222222222 "
                b"refs/heads/feat/work\0report-status\n"
            )
            + b"0000PACKpayload"
        )
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

    def test_delete_push_does_not_wait_for_client_eof(self) -> None:
        transport = FakeReceiveTransport()
        delete = (
            pkt(
                b"1111111111111111111111111111111111111111 "
                b"0000000000000000000000000000000000000000 "
                b"refs/heads/feat/work\0report-status\n"
            )
            + b"0000"
        )
        stdin = OpenDeleteRequestStream(
            b"capabilities\nconnect git-receive-pack\n" + delete
        )

        result = run_remote_helper(
            ["origin", "agent-broker://jj1xgo/agent-container"],
            {"AGENT_BROKER_REPOSITORY": "jj1xgo/agent-container"},
            transport,
            stdin,
            BytesIO(),
        )

        self.assertEqual(result, 0)
        self.assertEqual(transport.requests, [delete])

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
