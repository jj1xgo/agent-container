from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Callable, Mapping, Protocol
import urllib.error
import urllib.request

from agent_container.state import ensure_private_file


OPENSSL = "/usr/bin/openssl"
GITHUB_API = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
MAX_RESPONSE_BYTES = 1_048_576
TOKEN_REFRESH_MARGIN_SECONDS = 300
_CLIENT_ID = re.compile(r"^[A-Za-z0-9]{8,100}$")
_TOKEN_PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "checks": "read",
    "metadata": "read",
}


class Signer(Protocol):
    def sign(self, content: bytes, private_key: Path) -> bytes: ...


@dataclass(frozen=True)
class GitHubAppMetadata:
    client_id: str
    installation_id: int
    repository_id: int
    private_key: Path

    @classmethod
    def load(cls, metadata_path: Path, private_key: Path) -> "GitHubAppMetadata":
        _ensure_exact_private_file(metadata_path)
        _ensure_exact_private_file(private_key)
        try:
            body = metadata_path.read_text(encoding="utf-8")
            payload = json.loads(
                body,
                parse_constant=lambda _: (_ for _ in ()).throw(
                    ValueError("GitHub App metadata is invalid")
                ),
                object_pairs_hook=_object_without_duplicates,
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("GitHub App metadata is invalid") from None
        if not isinstance(payload, dict) or set(payload) != {
            "client_id",
            "installation_id",
            "repository_id",
        }:
            raise ValueError("GitHub App metadata is invalid")
        client_id = payload["client_id"]
        installation_id = payload["installation_id"]
        repository_id = payload["repository_id"]
        if not isinstance(client_id, str) or _CLIENT_ID.fullmatch(client_id) is None:
            raise ValueError("GitHub App metadata is invalid")
        for value in (installation_id, repository_id):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("GitHub App metadata is invalid")
        return cls(
            client_id=client_id,
            installation_id=installation_id,
            repository_id=repository_id,
            private_key=private_key.resolve(strict=True),
        )


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("GitHub JSON is invalid")
        result[key] = value
    return result


def _ensure_exact_private_file(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("GitHub App private path must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("GitHub App private path is invalid") from None
    if resolved != path:
        raise ValueError("GitHub App private path must not contain symlinks")
    return ensure_private_file(path)


def _b64url(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class OpenSSLSigner:
    executable: str = OPENSSL

    def sign(self, content: bytes, private_key: Path) -> bytes:
        if self.executable != OPENSSL:
            raise ValueError("OpenSSL executable must use the managed path")
        _ensure_exact_private_file(private_key)
        try:
            completed = subprocess.run(
                (
                    self.executable,
                    "dgst",
                    "-sha256",
                    "-sign",
                    str(private_key),
                ),
                input=content,
                capture_output=True,
                check=False,
                env={
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "OPENSSL_CONF": "/dev/null",
                },
            )
        except OSError:
            raise RuntimeError("GitHub App signing failed") from None
        if completed.returncode != 0 or not completed.stdout:
            raise RuntimeError("GitHub App signing failed")
        if len(completed.stdout) > 16_384:
            raise RuntimeError("GitHub App signing failed")
        return completed.stdout


def create_app_jwt(
    metadata: GitHubAppMetadata,
    signer: Signer,
    *,
    now: int | None = None,
) -> str:
    issued = int(time.time()) if now is None else now
    if isinstance(issued, bool) or not isinstance(issued, int) or issued < 0:
        raise ValueError("GitHub App clock is invalid")
    header = json.dumps(
        {"alg": "RS256", "typ": "JWT"}, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    claims = json.dumps(
        {"exp": issued + 540, "iat": issued - 60, "iss": metadata.client_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    signing_input = f"{_b64url(header)}.{_b64url(claims)}".encode("ascii")
    signature = signer.sign(signing_input, metadata.private_key)
    if not signature:
        raise RuntimeError("GitHub App signing failed")
    return f"{signing_input.decode('ascii')}.{_b64url(signature)}"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[str, Mapping[str, str], bytes], HttpResponse]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def github_transport(
    url: str, headers: Mapping[str, str], body: bytes
) -> HttpResponse:
    if not url.startswith(f"{GITHUB_API}/app/installations/") or not url.endswith(
        "/access_tokens"
    ):
        raise ValueError("GitHub App endpoint is not allowed")
    request = urllib.request.Request(
        url,
        data=body,
        headers=dict(headers),
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        response = opener.open(request, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError("GitHub App token request failed") from None
    try:
        response_body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(response_body) > MAX_RESPONSE_BYTES:
            raise RuntimeError("GitHub App token response is too large")
        return HttpResponse(
            status=response.status,
            headers=dict(response.headers.items()),
            body=response_body,
        )
    finally:
        response.close()


@dataclass(frozen=True)
class InstallationToken:
    token: str = field(repr=False)
    expires_at: int


def _parse_expiry(value: object) -> int:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("GitHub App token response is invalid")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        raise ValueError("GitHub App token response is invalid") from None
    return int(parsed.timestamp())


def _parse_token_response(
    response: HttpResponse, metadata: GitHubAppMetadata, now: int
) -> InstallationToken:
    if (
        isinstance(response.status, bool)
        or not isinstance(response.status, int)
        or not 100 <= response.status <= 599
    ):
        raise RuntimeError("GitHub App token request failed")
    if response.status != 201:
        raise RuntimeError(f"GitHub App token request failed with HTTP {response.status}")
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
    if content_type != "application/json":
        raise ValueError("GitHub App token response is invalid")
    try:
        payload = json.loads(
            response.body.decode("utf-8"),
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("GitHub App token response is invalid")
            ),
            object_pairs_hook=_object_without_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("GitHub App token response is invalid") from None
    if not isinstance(payload, dict):
        raise ValueError("GitHub App token response is invalid")
    token = payload.get("token")
    expires_at = _parse_expiry(payload.get("expires_at"))
    permissions = payload.get("permissions")
    repositories = payload.get("repositories")
    if (
        not isinstance(token, str)
        or not 16 <= len(token) <= 4096
        or any(ord(character) < 33 or ord(character) > 126 for character in token)
        or permissions != _TOKEN_PERMISSIONS
        or not isinstance(repositories, list)
        or len(repositories) != 1
        or not isinstance(repositories[0], dict)
        or repositories[0].get("id") != metadata.repository_id
        or expires_at <= now + TOKEN_REFRESH_MARGIN_SECONDS
    ):
        raise ValueError("GitHub App token response is invalid")
    return InstallationToken(token=token, expires_at=expires_at)


@dataclass
class InstallationTokenProvider:
    metadata: GitHubAppMetadata
    signer: Signer = field(default_factory=OpenSSLSigner)
    transport: Transport = github_transport
    clock: Callable[[], float] = time.time
    _cached: InstallationToken | None = field(default=None, init=False, repr=False)

    def get(self) -> InstallationToken:
        now = int(self.clock())
        if (
            self._cached is not None
            and self._cached.expires_at > now + TOKEN_REFRESH_MARGIN_SECONDS
        ):
            return self._cached
        jwt = create_app_jwt(self.metadata, self.signer, now=now)
        body = json.dumps(
            {
                "repository_ids": [self.metadata.repository_id],
                "permissions": _TOKEN_PERMISSIONS,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        response = self.transport(
            (
                f"{GITHUB_API}/app/installations/"
                f"{self.metadata.installation_id}/access_tokens"
            ),
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {jwt}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "User-Agent": "agent-container-github-broker",
            },
            body,
        )
        self._cached = _parse_token_response(response, self.metadata, now)
        return self._cached

    def invalidate(self) -> None:
        self._cached = None
