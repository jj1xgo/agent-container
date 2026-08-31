from base64 import urlsafe_b64encode
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, field
from datetime import datetime
import fcntl
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from typing import Callable, Iterator, Mapping, Protocol
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
from agent_container.github_app import Transport
from agent_container.github_app import github_transport
from agent_container.github_values import validate_repository_id
from agent_container.state import Repository


_CLIENT_ID = re.compile(r"^[A-Za-z0-9]{8,100}$")
_TOKEN_PERMISSIONS = {"issues": "write", "metadata": "read"}
_REPOSITORY_INVENTORY_URL = (
    f"{GITHUB_API}/installation/repositories?per_page=100"
)
_USER_AGENT = "agent-container-family-approval"
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
MAX_APP_METADATA_BYTES = 4096
MAX_PRIVATE_KEY_BYTES = 65_536
OPENSSL = "/usr/bin/openssl"


def _object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("family GitHub response is invalid")
        result[key] = value
    return result


FileIdentity = tuple[int, int]


def _same_identity(metadata: os.stat_result) -> FileIdentity:
    return metadata.st_dev, metadata.st_ino


def _validate_private_directory(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("family GitHub App metadata is invalid")
    if stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.getuid():
        raise PermissionError("family GitHub App private directory is not private")


def _validate_private_file(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("family GitHub App metadata is invalid")
    if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.getuid():
        raise PermissionError("family GitHub App private file is not private")


@contextmanager
def _open_directory(path: Path) -> Iterator[tuple[int, FileIdentity]]:
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise ValueError("family GitHub App metadata is invalid")
    with ExitStack() as descriptors:
        descriptor = os.open(
            os.sep, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC
        )
        descriptors.callback(os.close, descriptor)
        for component in path.parts[1:]:
            before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            child = os.open(
                component,
                os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
                dir_fd=descriptor,
            )
            descriptors.callback(os.close, child)
            opened = os.fstat(child)
            if _same_identity(before) != _same_identity(opened):
                raise ValueError("family GitHub App metadata is invalid")
            descriptor = child
        opened = os.fstat(descriptor)
        _validate_private_directory(opened)
        yield descriptor, _same_identity(opened)


@contextmanager
def _open_family_directory(
    layout: FamilyStateLayout,
) -> Iterator[tuple[int, FileIdentity, FileIdentity]]:
    with _open_directory(layout.root) as (root_descriptor, root_identity):
        before = os.stat("family", dir_fd=root_descriptor, follow_symlinks=False)
        family_descriptor = os.open(
            "family",
            os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
            dir_fd=root_descriptor,
        )
        with ExitStack() as descriptors:
            descriptors.callback(os.close, family_descriptor)
            opened = os.fstat(family_descriptor)
            if _same_identity(before) != _same_identity(opened):
                raise ValueError("family GitHub App metadata is invalid")
            _validate_private_directory(opened)
            yield family_descriptor, root_identity, _same_identity(opened)


def _read_all(descriptor: int, maximum_bytes: int) -> bytes:
    body = bytearray()
    while len(body) <= maximum_bytes:
        chunk = os.read(descriptor, min(4096, maximum_bytes + 1 - len(body)))
        if not chunk:
            break
        body.extend(chunk)
    if len(body) > maximum_bytes:
        raise ValueError("family GitHub App metadata is invalid")
    return bytes(body)


def _entry_stat(parent_descriptor: int, name: str) -> os.stat_result:
    metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    _validate_private_file(metadata)
    return metadata


@contextmanager
def _open_private_entry(
    parent_descriptor: int,
    name: str,
    *,
    maximum_bytes: int,
) -> Iterator[tuple[int, bytes, FileIdentity]]:
    before = _entry_stat(parent_descriptor, name)
    if before.st_size > maximum_bytes:
        raise ValueError("family GitHub App metadata is invalid")
    descriptor = os.open(
        name,
        os.O_RDONLY | _NOFOLLOW | _CLOEXEC,
        dir_fd=parent_descriptor,
    )
    with ExitStack() as descriptors:
        descriptors.callback(os.close, descriptor)
        opened = os.fstat(descriptor)
        _validate_private_file(opened)
        identity = _same_identity(opened)
        if _same_identity(before) != identity:
            raise ValueError("family GitHub App metadata is invalid")
        body = _read_all(descriptor, maximum_bytes)
        current = _entry_stat(parent_descriptor, name)
        if _same_identity(current) != identity:
            raise ValueError("family GitHub App metadata is invalid")
        os.lseek(descriptor, 0, os.SEEK_SET)
        yield descriptor, body, identity


def _decode_metadata(body: bytes) -> tuple[str, int]:
    try:
        payload = json.loads(
            body.decode("utf-8"),
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
        return client_id, installation_id
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        raise ValueError("family GitHub App metadata is invalid") from None


@dataclass(frozen=True, init=False)
class FamilyAppMetadata:
    client_id: str
    installation_id: int
    private_key: Path = field(repr=False)

    @classmethod
    def load(cls, layout: FamilyStateLayout) -> "FamilyAppMetadata":
        if cls is not FamilyAppMetadata or type(layout) is not FamilyStateLayout:
            raise ValueError("family GitHub App metadata is invalid")
        try:
            with _open_family_directory(layout) as (family_descriptor, _, _):
                with _open_private_entry(
                    family_descriptor,
                    "app.json",
                    maximum_bytes=MAX_APP_METADATA_BYTES,
                ) as (_, app_body, _), _open_private_entry(
                    family_descriptor,
                    "private-key.pem",
                    maximum_bytes=MAX_PRIVATE_KEY_BYTES,
                ) as (_, key_body, _):
                    client_id, installation_id = _decode_metadata(app_body)
            if not key_body:
                raise ValueError()
        except PermissionError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise ValueError("family GitHub App metadata is invalid") from None
        loaded = object.__new__(cls)
        object.__setattr__(loaded, "client_id", client_id)
        object.__setattr__(loaded, "installation_id", installation_id)
        object.__setattr__(loaded, "private_key", layout.family_private_key_file)
        return loaded


@dataclass(frozen=True)
class _SourceMaterial:
    metadata: FamilyAppMetadata
    family_descriptor: int
    app_descriptor: int
    key_descriptor: int
    root_identity: FileIdentity
    family_identity: FileIdentity
    app_identity: FileIdentity
    key_identity: FileIdentity
    app_body: bytes = field(repr=False)
    key_body: bytes = field(repr=False)


@contextmanager
def _open_current_material(
    layout: FamilyStateLayout,
) -> Iterator[_SourceMaterial]:
    if type(layout) is not FamilyStateLayout:
        raise ValueError("family GitHub App metadata is invalid")
    with _open_family_directory(layout) as (
        family_descriptor,
        root_identity,
        family_identity,
    ):
        with _open_private_entry(
            family_descriptor,
            "app.json",
            maximum_bytes=MAX_APP_METADATA_BYTES,
        ) as (app_descriptor, app_body, app_identity), _open_private_entry(
            family_descriptor,
            "private-key.pem",
            maximum_bytes=MAX_PRIVATE_KEY_BYTES,
        ) as (key_descriptor, key_body, key_identity):
            if not key_body:
                raise ValueError("family GitHub App metadata is invalid")
            client_id, installation_id = _decode_metadata(app_body)
            metadata = object.__new__(FamilyAppMetadata)
            object.__setattr__(metadata, "client_id", client_id)
            object.__setattr__(metadata, "installation_id", installation_id)
            object.__setattr__(metadata, "private_key", layout.family_private_key_file)
            yield _SourceMaterial(
                metadata,
                family_descriptor,
                app_descriptor,
                key_descriptor,
                root_identity,
                family_identity,
                app_identity,
                key_identity,
                app_body,
                key_body,
            )


def _write_all(descriptor: int, body: bytes) -> None:
    offset = 0
    while offset < len(body):
        written = os.write(descriptor, body[offset:])
        if written <= 0:
            raise ValueError()
        offset += written


def _validate_sealed_key(descriptor: int, expected_size: int) -> None:
    metadata = os.fstat(descriptor)
    required = (
        fcntl.F_SEAL_WRITE
        | fcntl.F_SEAL_GROW
        | fcntl.F_SEAL_SHRINK
        | fcntl.F_SEAL_SEAL
    )
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or metadata.st_size != expected_size
        or not 0 < expected_size <= MAX_PRIVATE_KEY_BYTES
        or fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) != required
    ):
        raise ValueError("family GitHub App metadata is invalid")


@contextmanager
def _sealed_key_snapshot(body: bytes) -> Iterator[int]:
    descriptor = os.memfd_create(
        "family-github-app-key",
        os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
    )
    with ExitStack() as descriptors:
        descriptors.callback(os.close, descriptor)
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, body)
        os.ftruncate(descriptor, len(body))
        os.lseek(descriptor, 0, os.SEEK_SET)
        required = (
            fcntl.F_SEAL_WRITE
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_SEAL
        )
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, required)
        _validate_sealed_key(descriptor, len(body))
        yield descriptor


class FamilySigner(Protocol):
    def sign(self, content: bytes, private_key_descriptor: int) -> bytes: ...


@dataclass(frozen=True)
class FamilyOpenSSLSigner:
    executable: str = OPENSSL

    def sign(self, content: bytes, private_key_descriptor: int) -> bytes:
        if self.executable != OPENSSL:
            raise ValueError("family GitHub App signing failed")
        try:
            metadata = os.fstat(private_key_descriptor)
            _validate_sealed_key(private_key_descriptor, metadata.st_size)
            completed = subprocess.run(
                (
                    self.executable,
                    "dgst",
                    "-sha256",
                    "-sign",
                    f"/proc/self/fd/{private_key_descriptor}",
                ),
                input=content,
                capture_output=True,
                check=False,
                pass_fds=(private_key_descriptor,),
                env={
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "OPENSSL_CONF": "/dev/null",
                },
            )
        except (OSError, TypeError, ValueError):
            raise RuntimeError("family GitHub App signing failed") from None
        if (
            completed.returncode != 0
            or not completed.stdout
            or len(completed.stdout) > 16_384
        ):
            raise RuntimeError("family GitHub App signing failed")
        return completed.stdout


def _b64url(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _create_family_jwt(
    metadata: FamilyAppMetadata,
    signer: FamilySigner,
    private_key_descriptor: int,
    *,
    now: int,
) -> str:
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise ValueError("family GitHub App clock is invalid")
    header = json.dumps(
        {"alg": "RS256", "typ": "JWT"}, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    claims = json.dumps(
        {"exp": now + 540, "iat": now - 60, "iss": metadata.client_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    signing_input = f"{_b64url(header)}.{_b64url(claims)}".encode("ascii")
    signature = signer.sign(signing_input, private_key_descriptor)
    if not signature:
        raise RuntimeError("family GitHub App signing failed")
    return f"{signing_input.decode('ascii')}.{_b64url(signature)}"


def _verify_material_after_sign(
    layout: FamilyStateLayout,
    material: _SourceMaterial,
) -> None:
    if (
        _same_identity(_entry_stat(material.family_descriptor, "app.json"))
        != material.app_identity
        or _same_identity(
            _entry_stat(material.family_descriptor, "private-key.pem")
        )
        != material.key_identity
    ):
        raise ValueError("family GitHub App metadata is invalid")
    for descriptor, expected, maximum in (
        (material.app_descriptor, material.app_body, MAX_APP_METADATA_BYTES),
        (material.key_descriptor, material.key_body, MAX_PRIVATE_KEY_BYTES),
    ):
        os.lseek(descriptor, 0, os.SEEK_SET)
        current = _read_all(descriptor, maximum)
        if len(current) != len(expected) or not hmac.compare_digest(
            hashlib.sha256(current).digest(), hashlib.sha256(expected).digest()
        ):
            raise ValueError("family GitHub App metadata is invalid")
    with _open_family_directory(layout) as (
        _,
        root_identity,
        family_identity,
    ):
        if (
            root_identity != material.root_identity
            or family_identity != material.family_identity
        ):
            raise ValueError("family GitHub App metadata is invalid")


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
    layout: FamilyStateLayout
    signer: FamilySigner = field(default_factory=FamilyOpenSSLSigner)
    transport: Transport = github_transport
    clock: Callable[[], float] = time.time
    _cached: InstallationToken | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.layout) is not FamilyStateLayout:
            raise ValueError("family GitHub App metadata is invalid")
        try:
            FamilyAppMetadata.load(self.layout)
        except PermissionError:
            raise ValueError("family GitHub App metadata is invalid") from None
        except (OSError, RuntimeError, TypeError, ValueError):
            raise ValueError("family GitHub App metadata is invalid") from None

    def get(self) -> InstallationToken:
        now = int(self.clock())
        try:
            with _open_current_material(self.layout) as material:
                if (
                    self._cached is not None
                    and self._cached.expires_at
                    > now + TOKEN_REFRESH_MARGIN_SECONDS
                ):
                    return self._cached
                with _sealed_key_snapshot(material.key_body) as key_descriptor:
                    try:
                        jwt = _create_family_jwt(
                            material.metadata,
                            self.signer,
                            key_descriptor,
                            now=now,
                        )
                    except (OSError, RuntimeError, TypeError, ValueError):
                        raise RuntimeError(
                            "family GitHub App signing failed"
                        ) from None
                    _verify_material_after_sign(self.layout, material)
                installation_id = material.metadata.installation_id
        except RuntimeError:
            raise
        except PermissionError:
            raise ValueError("family GitHub App metadata is invalid") from None
        except (OSError, TypeError, ValueError):
            raise ValueError("family GitHub App metadata is invalid") from None
        body = json.dumps(
            {"permissions": _TOKEN_PERMISSIONS},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        response = self.transport(
            (
                f"{GITHUB_API}/app/installations/"
                f"{installation_id}/access_tokens"
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
