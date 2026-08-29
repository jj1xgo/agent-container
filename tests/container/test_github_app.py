from base64 import urlsafe_b64decode
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from agent_container.github_app import GITHUB_API_VERSION
from agent_container.github_app import GitHubAppMetadata
from agent_container.github_app import HttpResponse
from agent_container.github_app import InstallationTokenProvider
from agent_container.github_app import MAX_RESPONSE_BYTES
from agent_container.github_app import OpenSSLSigner
from agent_container.github_app import create_app_jwt
from agent_container.github_app import github_transport


def decode_segment(value: str) -> dict[str, object]:
    padding = "=" * (-len(value) % 4)
    return json.loads(urlsafe_b64decode(value + padding))


class FakeSigner:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, Path]] = []

    def sign(self, content: bytes, private_key: Path) -> bytes:
        self.calls.append((content, private_key))
        return b"binary-signature"


class GitHubAppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.metadata_path = self.root / "app.json"
        self.private_key = self.root / "private-key.pem"
        self.metadata_path.write_text(
            json.dumps(
                {
                    "client_id": "Iv1abcdefghijk",
                    "installation_id": 123,
                    "repository_id": 456,
                }
            ),
            encoding="utf-8",
        )
        self.private_key.write_text("private-key-marker", encoding="utf-8")
        self.metadata_path.chmod(0o600)
        self.private_key.chmod(0o600)
        self.metadata = GitHubAppMetadata.load(self.metadata_path, self.private_key)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def response(
        self,
        token: str = "installation-token-marker",
        expires_at: str = "2026-08-25T14:00:00Z",
    ) -> HttpResponse:
        return HttpResponse(
            status=201,
            headers={"Content-Type": "application/json; charset=utf-8"},
            body=json.dumps(
                {
                    "token": token,
                    "expires_at": expires_at,
                    "permissions": {
                        "contents": "write",
                        "pull_requests": "write",
                        "checks": "read",
                        "issues": "read",
                        "metadata": "read",
                    },
                    "repositories": [{"id": 456, "name": "agent-container"}],
                }
            ).encode(),
        )

    def test_loads_exact_private_metadata_without_key_body(self) -> None:
        self.assertEqual(self.metadata.client_id, "Iv1abcdefghijk")
        self.assertEqual(self.metadata.installation_id, 123)
        self.assertEqual(self.metadata.repository_id, 456)
        self.assertEqual(self.metadata.private_key, self.private_key)

    def test_rejects_unsafe_metadata_or_private_key(self) -> None:
        cases = (
            {"client_id": "bad id"},
            {"installation_id": True},
            {"installation_id": 0},
            {"repository_id": -1},
            {"extra": 1},
        )
        baseline = {
            "client_id": "Iv1abcdefghijk",
            "installation_id": 123,
            "repository_id": 456,
        }
        for changes in cases:
            with self.subTest(changes=changes):
                self.metadata_path.write_text(
                    json.dumps(baseline | changes), encoding="utf-8"
                )
                self.metadata_path.chmod(0o600)
                with self.assertRaises(ValueError):
                    GitHubAppMetadata.load(self.metadata_path, self.private_key)

        self.metadata_path.write_text(json.dumps(baseline), encoding="utf-8")
        self.metadata_path.chmod(0o600)
        self.private_key.chmod(0o644)
        with self.assertRaises(PermissionError):
            GitHubAppMetadata.load(self.metadata_path, self.private_key)

    def test_rejects_relative_paths_and_symlinked_parent(self) -> None:
        with self.assertRaises(ValueError):
            GitHubAppMetadata.load(Path("app.json"), self.private_key)

        target = self.root / "target"
        target.mkdir()
        linked = self.root / "linked"
        linked.symlink_to(target, target_is_directory=True)
        linked_key = linked / "private-key.pem"
        (target / "private-key.pem").write_text("key", encoding="utf-8")
        (target / "private-key.pem").chmod(0o600)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            GitHubAppMetadata.load(self.metadata_path, linked_key)

    def test_creates_bounded_rs256_jwt_claims(self) -> None:
        signer = FakeSigner()
        token = create_app_jwt(self.metadata, signer, now=1_777_000_000)
        header, claims, signature = token.split(".")

        self.assertEqual(decode_segment(header), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(
            decode_segment(claims),
            {"exp": 1_777_000_540, "iat": 1_776_999_940, "iss": "Iv1abcdefghijk"},
        )
        self.assertTrue(signature)
        self.assertEqual(signer.calls[0][0], f"{header}.{claims}".encode())
        self.assertEqual(signer.calls[0][1], self.private_key)

    @mock.patch("agent_container.github_app.subprocess.run")
    def test_openssl_signer_uses_fixed_argv_and_sanitized_environment(
        self, run: mock.Mock
    ) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, b"signature", b"")
        signature = OpenSSLSigner().sign(b"content", self.private_key)
        self.assertEqual(signature, b"signature")
        call = run.call_args
        self.assertEqual(
            call.args[0],
            (
                "/usr/bin/openssl",
                "dgst",
                "-sha256",
                "-sign",
                str(self.private_key),
            ),
        )
        self.assertEqual(call.kwargs["input"], b"content")
        self.assertEqual(
            call.kwargs["env"],
            {
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "OPENSSL_CONF": "/dev/null",
            },
        )
        self.assertTrue(call.kwargs["capture_output"])

    def test_requests_exact_repository_and_permissions_then_caches_in_memory(self) -> None:
        calls: list[tuple[str, dict[str, str], bytes]] = []

        def transport(url, headers, body):  # type: ignore[no-untyped-def]
            calls.append((url, dict(headers), body))
            return self.response()

        provider = InstallationTokenProvider(
            self.metadata,
            signer=FakeSigner(),
            transport=transport,
            clock=lambda: datetime(2026, 8, 25, 13, 0, tzinfo=timezone.utc).timestamp(),
        )
        first = provider.get()
        second = provider.get()

        self.assertIs(first, second)
        self.assertEqual(first.token, "installation-token-marker")
        self.assertEqual(len(calls), 1)
        url, headers, body = calls[0]
        self.assertEqual(
            url, "https://api.github.com/app/installations/123/access_tokens"
        )
        self.assertEqual(headers["X-GitHub-Api-Version"], GITHUB_API_VERSION)
        self.assertTrue(headers["Authorization"].startswith("Bearer "))
        self.assertEqual(
            json.loads(body),
            {
                "repository_ids": [456],
                "permissions": {
                    "checks": "read",
                    "contents": "write",
                    "issues": "read",
                    "metadata": "read",
                    "pull_requests": "write",
                },
            },
        )

    def test_refreshes_near_expiry_and_after_invalidation(self) -> None:
        calls = 0

        def transport(*_):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            expiry = datetime.fromtimestamp(
                current[0] + 3600, tz=timezone.utc
            ).isoformat().replace("+00:00", "Z")
            return self.response(f"installation-token-{calls:02d}", expiry)

        current = [datetime(2026, 8, 25, 13, 0, tzinfo=timezone.utc).timestamp()]
        provider = InstallationTokenProvider(
            self.metadata,
            signer=FakeSigner(),
            transport=transport,
            clock=lambda: current[0],
        )
        self.assertEqual(provider.get().token, "installation-token-01")
        current[0] += 56 * 60
        self.assertEqual(provider.get().token, "installation-token-02")
        provider.invalidate()
        self.assertEqual(provider.get().token, "installation-token-03")

    def test_rejects_wrong_scope_expiry_content_type_and_secret_error_body(self) -> None:
        marker = "secret-response-marker"
        responses = (
            HttpResponse("secret-response-marker", {}, b""),  # type: ignore[arg-type]
            HttpResponse(401, {"Content-Type": "application/json"}, marker.encode()),
            HttpResponse(201, {"Content-Type": "text/plain"}, marker.encode()),
            HttpResponse(
                201,
                {"Content-Type": "application/json"},
                self.response().body.replace(b'"id": 456', b'"id": 999'),
            ),
            HttpResponse(
                201,
                {"Content-Type": "application/json"},
                self.response().body.replace(b'"contents": "write"', b'"contents": "read"'),
            ),
            HttpResponse(
                201,
                {"Content-Type": "application/json"},
                self.response().body.replace(
                    b"2026-08-25T14:00:00Z", b"2026-08-25T13:01:00Z"
                ),
            ),
        )
        for response in responses:
            with self.subTest(status=response.status):
                provider = InstallationTokenProvider(
                    self.metadata,
                    signer=FakeSigner(),
                    transport=lambda *_: response,
                    clock=lambda: datetime(
                        2026, 8, 25, 13, 0, tzinfo=timezone.utc
                    ).timestamp(),
                )
                with self.assertRaises((ValueError, RuntimeError)) as raised:
                    provider.get()
                self.assertNotIn(marker, str(raised.exception))
                self.assertIsNone(provider._cached)

    def test_rejects_missing_extra_or_write_issue_permissions_without_echoing_marker(
        self,
    ) -> None:
        marker = "secret-response-marker"
        valid = json.loads(self.response().body)
        valid["marker"] = marker
        cases = (
            {
                **valid,
                "permissions": {
                    k: v for k, v in valid["permissions"].items() if k != "issues"
                },
            },
            {
                **valid,
                "permissions": valid["permissions"] | {"administration": "read"},
            },
            {**valid, "permissions": valid["permissions"] | {"issues": "write"}},
        )
        for payload in cases:
            with self.subTest(permissions=payload["permissions"]):
                response = HttpResponse(
                    201,
                    {"Content-Type": "application/json"},
                    json.dumps(payload).encode(),
                )
                provider = InstallationTokenProvider(
                    self.metadata,
                    signer=FakeSigner(),
                    transport=lambda *_: response,
                    clock=lambda: datetime(
                        2026, 8, 25, 13, 0, tzinfo=timezone.utc
                    ).timestamp(),
                )
                with self.assertRaises(ValueError) as raised:
                    provider.get()
                self.assertNotIn(marker, str(raised.exception))
                self.assertIsNone(provider._cached)

    def test_token_repr_does_not_include_secret(self) -> None:
        provider = InstallationTokenProvider(
            self.metadata,
            signer=FakeSigner(),
            transport=lambda *_: self.response(),
            clock=lambda: datetime(2026, 8, 25, 13, 0, tzinfo=timezone.utc).timestamp(),
        )
        token = provider.get()
        self.assertNotIn(token.token, repr(token))

    def test_token_transport_sanitizes_protocol_errors_from_all_io_phases(
        self,
    ) -> None:
        marker = "secret-malformed-token-status"

        class ProtocolResponse:
            status = 201
            headers = {"Content-Type": "application/json"}

            def __init__(self, phase: str) -> None:
                self.phase = phase

            def read(self, maximum: int) -> bytes:
                self.assert_maximum(maximum)
                if self.phase == "read":
                    raise http.client.BadStatusLine(marker)
                return b"{}"

            def assert_maximum(self, maximum: int) -> None:
                if maximum != MAX_RESPONSE_BYTES + 1:
                    raise AssertionError("unexpected token response read bound")

            def close(self) -> None:
                if self.phase == "close":
                    raise http.client.BadStatusLine(marker)

        class ProtocolOpener:
            def __init__(self, phase: str) -> None:
                self.phase = phase

            def open(self, request, timeout):  # type: ignore[no-untyped-def]
                if self.phase == "open":
                    raise http.client.BadStatusLine(marker)
                return ProtocolResponse(self.phase)

        url = "https://api.github.com/app/installations/123/access_tokens"
        for phase in ("open", "read", "close"):
            with self.subTest(phase=phase):
                with mock.patch(
                    "agent_container.github_app.urllib.request.build_opener",
                    return_value=ProtocolOpener(phase),
                ):
                    try:
                        github_transport(url, {}, b"{}")
                    except RuntimeError as error:
                        self.assertEqual(
                            str(error), "GitHub App token request failed"
                        )
                        self.assertNotIn(marker, str(error))
                        self.assertNotIn(marker, repr(error))
                        self.assertIsNone(error.__cause__)
                        self.assertTrue(error.__suppress_context__)
                    except Exception as error:  # noqa: BLE001
                        self.fail(
                            f"{phase} leaked {type(error).__name__}: {error}"
                        )
                    else:
                        self.fail(f"{phase} protocol failure was accepted")
