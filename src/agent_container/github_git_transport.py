from dataclasses import dataclass
from base64 import b64encode
from typing import BinaryIO, Callable, Iterable, Mapping, Protocol
import urllib.error
import urllib.request

from agent_container.github_app import InstallationToken
from agent_container.github_app import InstallationTokenProvider
from agent_container.github_broker_error import BrokerStageError
from agent_container.state import Repository


GITHUB = "https://github.com"
MAX_DISCOVERY_BYTES = 1_048_576
MAX_UPLOAD_PACK_REQUEST_BYTES = 16 * 1024 * 1024
MAX_UPLOAD_PACK_RESPONSE_BYTES = 8 * 1024 * 1024 * 1024
MAX_RECEIVE_PACK_ADVERTISEMENT_BYTES = 4 * 1024 * 1024
MAX_RECEIVE_PACK_REQUEST_BYTES = 2 * 1024 * 1024 * 1024
MAX_RECEIVE_PACK_RESPONSE_BYTES = 16 * 1024 * 1024
READ_CHUNK_BYTES = 65_536
_RECEIVE_PACK_PREAMBLE = b"001f# service=git-receive-pack\n0000"
_UPLOAD_PACK_PREAMBLE = b"001e# service=git-upload-pack\n0000"


def _get_installation_token(
    tokens: InstallationTokenProvider,
) -> InstallationToken:
    try:
        return tokens.get()
    except (BrokerStageError, ValueError, RuntimeError, OSError):
        raise BrokerStageError("token") from None


def _invalidate_installation_token(tokens: InstallationTokenProvider) -> None:
    try:
        tokens.invalidate()
    except (BrokerStageError, ValueError, RuntimeError, OSError):
        raise BrokerStageError("token") from None


def _close_http_response(response: "HttpStream", stage: str) -> None:
    try:
        response.close()
    except (ValueError, RuntimeError, OSError):
        raise BrokerStageError(stage) from None


class HttpStream(Protocol):
    status: int
    headers: Mapping[str, str]

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


OpenHttp = Callable[[str, str, Mapping[str, str], bytes | None], HttpStream]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def open_github_http(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
) -> HttpStream:
    if method not in {"GET", "POST"} or not url.startswith(f"{GITHUB}/"):
        raise ValueError("GitHub Git endpoint is not allowed")
    request = urllib.request.Request(
        url,
        data=body,
        headers=dict(headers),
        method=method,
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        return opener.open(request, timeout=60)
    except urllib.error.HTTPError as error:
        return error
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError("GitHub Git request failed") from None


@dataclass
class GitHubUploadPackTransport:
    repository: Repository
    tokens: InstallationTokenProvider
    open_http: OpenHttp = open_github_http

    @property
    def repository_url(self) -> str:
        return f"{GITHUB}/{self.repository.slug}.git"

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/x-git-upload-pack-advertisement",
            "Cache-Control": "no-cache",
            "Git-Protocol": "version=2",
            "User-Agent": "agent-container-github-broker",
        }

    def _open(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        stage: str,
    ) -> HttpStream:
        for attempt in range(2):
            token = _get_installation_token(self.tokens)
            authorized = dict(headers)
            basic = b64encode(f"x-access-token:{token.token}".encode("ascii")).decode(
                "ascii"
            )
            authorized["Authorization"] = f"Basic {basic}"
            try:
                response = self.open_http(method, url, authorized, body)
            except (RuntimeError, OSError):
                raise BrokerStageError(stage) from None
            if response.status != 401 or attempt == 1:
                return response
            _close_http_response(response, stage)
            _invalidate_installation_token(self.tokens)
        raise AssertionError("unreachable")

    def discover(self) -> bytes:
        headers = self._headers()
        response = self._open(
            "GET",
            f"{self.repository_url}/info/refs?service=git-upload-pack",
            headers,
            None,
            "upload-discovery",
        )
        try:
            try:
                self._validate_response(
                    response,
                    expected="application/x-git-upload-pack-advertisement",
                )
                body = response.read(MAX_DISCOVERY_BYTES + 1)
                if not body or len(body) > MAX_DISCOVERY_BYTES:
                    raise ValueError("GitHub upload-pack advertisement is invalid")
                if not body.startswith(_UPLOAD_PACK_PREAMBLE):
                    raise ValueError("GitHub upload-pack advertisement is invalid")
                advertisement = body[len(_UPLOAD_PACK_PREAMBLE) :]
                if not advertisement.startswith(b"000eversion 2\n"):
                    raise ValueError("GitHub upload-pack did not negotiate protocol v2")
                return advertisement
            except (ValueError, RuntimeError, OSError):
                raise BrokerStageError("upload-discovery") from None
        finally:
            _close_http_response(response, "upload-discovery")

    def rpc(self, request: bytes) -> Iterable[bytes]:
        if (
            not isinstance(request, bytes)
            or not request
            or len(request) > MAX_UPLOAD_PACK_REQUEST_BYTES
            or not request.endswith(b"0000")
        ):
            raise ValueError("GitHub upload-pack request is invalid")
        headers = self._headers()
        headers["Accept"] = "application/x-git-upload-pack-result"
        headers["Content-Type"] = "application/x-git-upload-pack-request"
        response = self._open(
            "POST",
            f"{self.repository_url}/git-upload-pack",
            headers,
            request,
            "upload-rpc",
        )
        try:
            try:
                self._validate_response(
                    response,
                    expected="application/x-git-upload-pack-result",
                )
                transferred = 0
                pending = b""
                while True:
                    chunk = response.read(READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    transferred += len(chunk)
                    if transferred > MAX_UPLOAD_PACK_RESPONSE_BYTES:
                        raise ValueError("GitHub upload-pack response is too large")
                    buffered = pending + chunk
                    if len(buffered) > 4:
                        yield buffered[:-4]
                        pending = buffered[-4:]
                    else:
                        pending = buffered
                if pending != b"0000":
                    raise ValueError("GitHub upload-pack response is incomplete")
                yield b"0002"
            except (ValueError, RuntimeError, OSError):
                raise BrokerStageError("upload-rpc") from None
        finally:
            _close_http_response(response, "upload-rpc")

    @staticmethod
    def _validate_response(response: HttpStream, *, expected: str) -> None:
        if (
            isinstance(response.status, bool)
            or not isinstance(response.status, int)
            or response.status != 200
        ):
            raise RuntimeError("GitHub Git request failed")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type.strip() != expected:
            raise ValueError("GitHub Git response content type is invalid")


@dataclass
class GitHubReceivePackTransport:
    repository: Repository
    tokens: InstallationTokenProvider
    open_http: OpenHttp = open_github_http

    @property
    def repository_url(self) -> str:
        return f"{GITHUB}/{self.repository.slug}.git"

    def _open(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        stage: str,
    ) -> HttpStream:
        for attempt in range(2):
            token = _get_installation_token(self.tokens)
            authorized = dict(headers)
            basic = b64encode(f"x-access-token:{token.token}".encode("ascii")).decode(
                "ascii"
            )
            authorized["Authorization"] = f"Basic {basic}"
            try:
                response = self.open_http(method, url, authorized, body)
            except (RuntimeError, OSError):
                raise BrokerStageError(stage) from None
            if response.status != 401 or attempt == 1:
                return response
            _close_http_response(response, stage)
            _invalidate_installation_token(self.tokens)
        raise AssertionError("unreachable")

    def discover(self) -> bytes:
        response = self._open(
            "GET",
            f"{self.repository_url}/info/refs?service=git-receive-pack",
            {
                "Accept": "application/x-git-receive-pack-advertisement",
                "Cache-Control": "no-cache",
                "User-Agent": "agent-container-github-broker",
            },
            None,
            "receive-discovery",
        )
        try:
            try:
                GitHubUploadPackTransport._validate_response(
                    response,
                    expected="application/x-git-receive-pack-advertisement",
                )
                body = response.read(MAX_RECEIVE_PACK_ADVERTISEMENT_BYTES + 1)
                if (
                    len(body) > MAX_RECEIVE_PACK_ADVERTISEMENT_BYTES
                    or not body.startswith(_RECEIVE_PACK_PREAMBLE)
                ):
                    raise ValueError("GitHub receive-pack advertisement is invalid")
                advertisement = body[len(_RECEIVE_PACK_PREAMBLE) :]
                if not advertisement:
                    raise ValueError("GitHub receive-pack advertisement is invalid")
                return advertisement
            except (ValueError, RuntimeError, OSError):
                raise BrokerStageError("receive-discovery") from None
        finally:
            _close_http_response(response, "receive-discovery")

    def rpc(self, request: bytes) -> Iterable[bytes]:
        if (
            not isinstance(request, bytes)
            or not request
            or len(request) > MAX_RECEIVE_PACK_REQUEST_BYTES
        ):
            raise ValueError("GitHub receive-pack request is invalid")
        response = self._open(
            "POST",
            f"{self.repository_url}/git-receive-pack",
            {
                "Accept": "application/x-git-receive-pack-result",
                "Cache-Control": "no-cache",
                "Content-Type": "application/x-git-receive-pack-request",
                "User-Agent": "agent-container-github-broker",
            },
            request,
            "receive-rpc",
        )
        try:
            try:
                GitHubUploadPackTransport._validate_response(
                    response, expected="application/x-git-receive-pack-result"
                )
                transferred = 0
                while True:
                    chunk = response.read(READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    transferred += len(chunk)
                    if transferred > MAX_RECEIVE_PACK_RESPONSE_BYTES:
                        raise ValueError("GitHub receive-pack response is too large")
                    yield chunk
                if transferred == 0:
                    raise ValueError("GitHub receive-pack response is invalid")
            except (ValueError, RuntimeError, OSError):
                raise BrokerStageError("receive-rpc") from None
        finally:
            _close_http_response(response, "receive-rpc")
