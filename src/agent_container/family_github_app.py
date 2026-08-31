from dataclasses import dataclass, field
from datetime import datetime
import http.client
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Callable, Mapping
import urllib.error
import urllib.request

from agent_container.family_state import FamilyBinding
from agent_container.family_state import FamilyStateLayout
from agent_container.github_app import GITHUB_API
from agent_container.github_app import GITHUB_API_VERSION
from agent_container.github_app import MAX_RESPONSE_BYTES
from agent_container.github_app import TOKEN_REFRESH_MARGIN_SECONDS
from agent_container.github_app import HttpResponse
from agent_container.github_app import InstallationToken
from agent_container.github_app import OpenSSLSigner
from agent_container.github_app import Signer
from agent_container.github_app import Transport
from agent_container.github_app import create_app_jwt
from agent_container.github_app import github_transport
from agent_container.github_values import validate_repository_id
from agent_container.state import Repository


_CLIENT_ID = re.compile(r"^[A-Za-z0-9]{8,100}$")
_TOKEN_PERMISSIONS = {"issues": "write", "metadata": "read"}
_REPOSITORY_INVENTORY_URL = (
    f"{GITHUB_API}/installation/repositories?per_page=100"
)
_USER_AGENT = "agent-container-family-approval"


def _object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("family GitHub response is invalid")
        result[key] = value
    return result


def _ensure_exact_private_file(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("family GitHub App private path is invalid")
    try:
        resolved = path.resolve(strict=True)
        metadata = path.stat()
    except (OSError, RuntimeError):
        raise ValueError("family GitHub App private path is invalid") from None
    if resolved != path or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("family GitHub App private path is invalid")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise PermissionError("family GitHub App private file is not private")
    if metadata.st_uid != os.getuid():
        raise PermissionError("family GitHub App private file is not private")
    return resolved


def _ensure_exact_private_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("family GitHub App private path is invalid")
    try:
        resolved = path.resolve(strict=True)
        metadata = path.stat()
    except (OSError, RuntimeError):
        raise ValueError("family GitHub App private path is invalid") from None
    if resolved != path or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("family GitHub App private path is invalid")
    if stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.getuid():
        raise PermissionError("family GitHub App private directory is not private")
    return resolved


@dataclass(frozen=True)
class FamilyAppMetadata:
    client_id: str
    installation_id: int
    private_key: Path = field(repr=False)

    @classmethod
    def load(cls, layout: FamilyStateLayout) -> "FamilyAppMetadata":
        if type(layout) is not FamilyStateLayout:
            raise ValueError("family GitHub App metadata is invalid")
        _ensure_exact_private_directory(layout.root)
        _ensure_exact_private_directory(layout.family_root)
        metadata_path = _ensure_exact_private_file(layout.family_app_file)
        private_key = _ensure_exact_private_file(layout.family_private_key_file)
        try:
            payload = json.loads(
                metadata_path.read_text(encoding="utf-8"),
                object_pairs_hook=_object_without_duplicates,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
            if not isinstance(payload, dict) or set(payload) != {
                "client_id",
                "installation_id",
            }:
                raise ValueError()
            client_id = payload["client_id"]
            installation_id = payload["installation_id"]
            if (
                not isinstance(client_id, str)
                or _CLIENT_ID.fullmatch(client_id) is None
                or isinstance(installation_id, bool)
                or not isinstance(installation_id, int)
                or installation_id <= 0
            ):
                raise ValueError()
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            raise ValueError("family GitHub App metadata is invalid") from None
        return cls(client_id, installation_id, private_key)


def _parse_expiry(value: object) -> int:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("family GitHub App token response is invalid")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        raise ValueError("family GitHub App token response is invalid") from None
    return int(parsed.timestamp())


def _json(response: HttpResponse, *, expected_status: int, label: str) -> object:
    status = response.status
    if (
        isinstance(status, bool)
        or not isinstance(status, int)
        or not 100 <= status <= 599
    ):
        raise RuntimeError(f"family GitHub {label} request failed")
    if status != expected_status:
        raise RuntimeError(f"family GitHub {label} request failed")
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise RuntimeError(f"family GitHub {label} response is too large")
    if (
        response.headers.get("Content-Type", "").split(";", 1)[0].strip()
        != "application/json"
    ):
        raise ValueError(f"family GitHub {label} response is invalid")
    try:
        return json.loads(
            response.body.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        raise ValueError(f"family GitHub {label} response is invalid") from None


def _parse_token_response(
    response: HttpResponse, now: int
) -> InstallationToken:
    payload = _json(response, expected_status=201, label="App token")
    if not isinstance(payload, dict):
        raise ValueError("family GitHub App token response is invalid")
    token = payload.get("token")
    expires_at = _parse_expiry(payload.get("expires_at"))
    if (
        not isinstance(token, str)
        or not 16 <= len(token) <= 4096
        or any(ord(character) < 33 or ord(character) > 126 for character in token)
        or payload.get("permissions") != _TOKEN_PERMISSIONS
        or payload.get("repository_selection") != "selected"
        or expires_at <= now + TOKEN_REFRESH_MARGIN_SECONDS
    ):
        raise ValueError("family GitHub App token response is invalid")
    return InstallationToken(token, expires_at)


@dataclass
class FamilyInstallationTokenProvider:
    metadata: FamilyAppMetadata
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
        jwt = create_app_jwt(self.metadata, self.signer, now=now)  # type: ignore[arg-type]
        body = json.dumps(
            {"permissions": _TOKEN_PERMISSIONS},
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
                "User-Agent": _USER_AGENT,
            },
            body,
        )
        self._cached = _parse_token_response(response, now)
        return self._cached

    def invalidate(self) -> None:
        self._cached = None


FamilyRepositoryTransport = Callable[
    [str, str, Mapping[str, str], bytes | None], HttpResponse
]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def family_repository_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
) -> HttpResponse:
    if method != "GET" or url != _REPOSITORY_INVENTORY_URL or body is not None:
        raise ValueError("family GitHub repository endpoint is not allowed")
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        try:
            response = opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        try:
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RESPONSE_BYTES:
                raise RuntimeError("family GitHub repository response is too large")
            return HttpResponse(
                status=response.status,
                headers=dict(response.headers.items()),
                body=response_body,
            )
        finally:
            response.close()
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        http.client.HTTPException,
    ):
        raise RuntimeError("family GitHub repository request failed") from None


def _validate_binding(binding: FamilyBinding) -> FamilyBinding:
    if type(binding) is not FamilyBinding or type(binding.repository) is not Repository:
        raise ValueError("family binding is invalid")
    repository = binding.repository
    try:
        parsed = Repository.parse(repository.slug)
        repository_id = validate_repository_id(binding.repository_id)
    except ValueError:
        raise ValueError("family binding is invalid") from None
    if parsed != repository or repository.slug != repository.slug.lower():
        raise ValueError("family binding is invalid")
    return FamilyBinding(repository, repository_id)


def _has_pagination(headers: Mapping[str, str]) -> bool:
    return any(key.lower() == "link" and bool(value.strip()) for key, value in headers.items())


def verify_family_repository(
    token: InstallationToken,
    binding: FamilyBinding,
    transport: FamilyRepositoryTransport = family_repository_transport,
) -> None:
    binding = _validate_binding(binding)
    if (
        type(token) is not InstallationToken
        or not isinstance(token.token, str)
        or not 16 <= len(token.token) <= 4096
        or any(ord(character) < 33 or ord(character) > 126 for character in token.token)
    ):
        raise ValueError("family GitHub installation token is invalid")
    try:
        response = transport(
            "GET",
            _REPOSITORY_INVENTORY_URL,
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token.token}",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "User-Agent": _USER_AGENT,
            },
            None,
        )
    except (ValueError, RuntimeError, OSError, http.client.HTTPException):
        raise RuntimeError("family GitHub repository request failed") from None
    if _has_pagination(response.headers):
        raise ValueError("family GitHub repository inventory is invalid")
    payload = _json(response, expected_status=200, label="repository inventory")
    if not isinstance(payload, dict):
        raise ValueError("family GitHub repository inventory is invalid")
    total_count = payload.get("total_count")
    repositories = payload.get("repositories")
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count != 1
        or not isinstance(repositories, list)
        or len(repositories) != 1
        or not isinstance(repositories[0], dict)
    ):
        raise ValueError("family GitHub repository inventory is invalid")
    repository = repositories[0]
    repository_id = repository.get("id")
    full_name = repository.get("full_name")
    if (
        isinstance(repository_id, bool)
        or not isinstance(repository_id, int)
        or repository_id <= 0
        or repository_id != binding.repository_id
        or not isinstance(full_name, str)
        or full_name != binding.repository.slug
    ):
        raise ValueError("family GitHub repository inventory is invalid")
