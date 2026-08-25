from datetime import datetime, timezone
from io import BytesIO
import unittest

from agent_container.github_app import InstallationToken
from agent_container.github_git_transport import GitHubUploadPackTransport
from agent_container.github_git_transport import GitHubReceivePackTransport
from agent_container.state import Repository


class FakeTokens:
    def __init__(self) -> None:
        self.count = 0
        self.invalidations = 0

    def get(self) -> InstallationToken:
        self.count += 1
        return InstallationToken(
            token=f"secret-installation-token-{self.count}",
            expires_at=int(
                datetime(2026, 8, 25, 14, 0, tzinfo=timezone.utc).timestamp()
            ),
        )

    def invalidate(self) -> None:
        self.invalidations += 1


class FakeResponse:
    def __init__(self, status: int, content_type: str, body: bytes) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.stream = BytesIO(body)
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)

    def close(self) -> None:
        self.closed = True


class GitHubUploadPackTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tokens = FakeTokens()
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []
        self.responses: list[FakeResponse] = []

        def open_http(method, url, headers, body):  # type: ignore[no-untyped-def]
            self.calls.append((method, url, dict(headers), body))
            return self.responses.pop(0)

        self.transport = GitHubUploadPackTransport(
            Repository.parse("jj1xgo/agent-container"),
            self.tokens,  # type: ignore[arg-type]
            open_http,
        )

    def test_discovers_exact_repository_with_protocol_v2(self) -> None:
        advertisement = b"000eversion 2\n0000"
        response = FakeResponse(
            200, "application/x-git-upload-pack-advertisement", advertisement
        )
        self.responses.append(response)

        self.assertEqual(self.transport.discover(), advertisement)

        method, url, headers, body = self.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(
            url,
            "https://github.com/jj1xgo/agent-container.git/info/refs?service=git-upload-pack",
        )
        self.assertEqual(headers["Git-Protocol"], "version=2")
        self.assertTrue(headers["Authorization"].startswith("Basic "))
        self.assertIsNone(body)
        self.assertTrue(response.closed)

    def test_streams_upload_pack_response_in_chunks(self) -> None:
        request = b"0009done\n0000"
        response = FakeResponse(
            200,
            "application/x-git-upload-pack-result",
            b"x" * 69_996 + b"0002",
        )
        self.responses.append(response)

        chunks = list(self.transport.rpc(request))

        self.assertEqual([len(chunk) for chunk in chunks], [65_536, 4_464])
        method, url, headers, body = self.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(
            url,
            "https://github.com/jj1xgo/agent-container.git/git-upload-pack",
        )
        self.assertEqual(
            headers["Content-Type"], "application/x-git-upload-pack-request"
        )
        self.assertEqual(body, request)
        self.assertTrue(response.closed)

    def test_retries_401_once_with_invalidated_token(self) -> None:
        first = FakeResponse(401, "application/json", b"secret-error-body")
        second = FakeResponse(
            200,
            "application/x-git-upload-pack-advertisement",
            b"000eversion 2\n0000",
        )
        self.responses.extend((first, second))

        self.transport.discover()

        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.tokens.invalidations, 1)
        self.assertNotEqual(
            self.calls[0][2]["Authorization"], self.calls[1][2]["Authorization"]
        )
        self.assertTrue(first.closed)

    def test_rejects_second_401_wrong_content_type_and_empty_discovery(self) -> None:
        cases = (
            [
                FakeResponse(401, "application/json", b"secret-marker"),
                FakeResponse(401, "application/json", b"secret-marker"),
            ],
            [FakeResponse(200, "text/plain", b"secret-marker")],
            [
                FakeResponse(
                    200, "application/x-git-upload-pack-advertisement", b""
                )
            ],
        )
        for responses in cases:
            with self.subTest(status=responses[-1].status):
                self.responses[:] = responses
                with self.assertRaises((ValueError, RuntimeError)) as raised:
                    self.transport.discover()
                self.assertNotIn("secret-marker", str(raised.exception))
                self.calls.clear()

    def test_rejects_invalid_rpc_before_http(self) -> None:
        for request in (b"", b"not-flushed"):
            with self.subTest(request=request):
                with self.assertRaises(ValueError):
                    list(self.transport.rpc(request))
        self.assertEqual(self.calls, [])


class GitHubReceivePackTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tokens = FakeTokens()
        self.calls = []
        self.responses: list[FakeResponse] = []

        def open_http(method, url, headers, body):  # type: ignore[no-untyped-def]
            self.calls.append((method, url, dict(headers), body))
            return self.responses.pop(0)

        self.transport = GitHubReceivePackTransport(
            Repository.parse("jj1xgo/agent-container"),
            self.tokens,  # type: ignore[arg-type]
            open_http,
        )

    def test_strips_exact_smart_http_preamble_from_advertisement(self) -> None:
        refs = b"00b1" + b"1" * 40 + b" refs/heads/feat/test\0report-status\n0000"
        response = FakeResponse(
            200,
            "application/x-git-receive-pack-advertisement",
            b"001f# service=git-receive-pack\n0000" + refs,
        )
        self.responses.append(response)

        self.assertEqual(self.transport.discover(), refs)
        method, url, headers, body = self.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(
            url,
            "https://github.com/jj1xgo/agent-container.git/info/refs?service=git-receive-pack",
        )
        self.assertIn("Authorization", headers)
        self.assertIsNone(body)
        self.assertTrue(response.closed)

    def test_posts_receive_pack_only_to_fixed_endpoint(self) -> None:
        request = b"commands-and-pack"
        response = FakeResponse(
            200, "application/x-git-receive-pack-result", b"000eunpack ok\n0000"
        )
        self.responses.append(response)

        self.assertEqual(list(self.transport.rpc(request)), [b"000eunpack ok\n0000"])
        method, url, headers, body = self.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(
            url, "https://github.com/jj1xgo/agent-container.git/git-receive-pack"
        )
        self.assertEqual(
            headers["Content-Type"], "application/x-git-receive-pack-request"
        )
        self.assertEqual(body, request)
        self.assertTrue(response.closed)

    def test_retries_401_once_and_rejects_malformed_or_empty_responses(self) -> None:
        first = FakeResponse(401, "application/json", b"secret-marker")
        second = FakeResponse(
            200,
            "application/x-git-receive-pack-advertisement",
            b"001f# service=git-receive-pack\n0000refs",
        )
        self.responses.extend((first, second))
        self.assertEqual(self.transport.discover(), b"refs")
        self.assertEqual(self.tokens.invalidations, 1)
        self.assertTrue(first.closed)

        for body in (b"wrong", b"001f# service=git-receive-pack\n0000"):
            self.responses[:] = [
                FakeResponse(
                    200, "application/x-git-receive-pack-advertisement", body
                )
            ]
            with self.assertRaises(ValueError) as raised:
                self.transport.discover()
            self.assertNotIn("secret-marker", str(raised.exception))

        self.responses[:] = [
            FakeResponse(200, "application/x-git-receive-pack-result", b"")
        ]
        with self.assertRaises(ValueError):
            list(self.transport.rpc(b"request"))
