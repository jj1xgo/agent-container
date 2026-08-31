"""One-shot host-only GitHub transport for approved family Issues."""

from dataclasses import dataclass, field
import http.client
import json
import re
from typing import Callable, Mapping

from agent_container.family_github_app import FamilyInstallationTokenProvider
from agent_container.family_issue import CanonicalFamilyIssue
from agent_container.family_state import FamilyBinding
from agent_container.github_app import GITHUB_API_VERSION
from agent_container.github_app import HttpResponse
from agent_container.github_app import InstallationToken
from agent_container.github_values import validate_issue_number
from agent_container.github_values import validate_repository_id
from agent_container.state import Repository


MAX_ISSUE_RESPONSE_BYTES = 128 * 1024
_MAX_CANONICAL_BODY_BYTES = 64 * 1024
_MAX_ISSUE_REQUEST_BYTES = 512 * 1024
_API_ORIGIN = "https://api.github.com"
_API_HOST = "api.github.com"
_USER_AGENT = "agent-container-family-approval"
_REPOSITORY_PART = r"[a-z0-9][a-z0-9._-]{0,99}"
_CREATE_ENDPOINT = re.compile(
    rf"^https://api\.github\.com/repos/({_REPOSITORY_PART})/"
    rf"({_REPOSITORY_PART})/issues$"
)
_ISSUE_ENDPOINT = re.compile(
    rf"^https://api\.github\.com/repos/({_REPOSITORY_PART})/"
    rf"({_REPOSITORY_PART})/issues/([1-9][0-9]*)$"
)
_POST_HEADER_NAMES = frozenset(
    {
        "Accept",
        "Authorization",
        "Content-Type",
        "X-GitHub-Api-Version",
        "User-Agent",
    }
)
_GET_HEADER_NAMES = _POST_HEADER_NAMES - {"Content-Type"}
_KNOWN_TRANSPORT_ERRORS = (
    ValueError,
    RuntimeError,
    TimeoutError,
    OSError,
    http.client.HTTPException,
)


@dataclass(frozen=True)
class CreatedIssue:
    number: int
    url: str


class SendNotStarted(RuntimeError):
    """An Issue POST failure proven to precede request-body transmission."""

    _STAGES = frozenset({"token", "send"})

    def __init__(self, stage: str) -> None:
        if type(stage) is not str or stage not in self._STAGES:
            raise ValueError("family Issue failure stage is invalid")
        self.stage = stage
        RuntimeError.__init__(self, "family Issue send did not start")


class SendOutcomeUnknown(RuntimeError):
    """An Issue POST whose remote result cannot be proven."""

    _STAGES = frozenset({"send", "response"})

    def __init__(self, stage: str) -> None:
        if type(stage) is not str or stage not in self._STAGES:
            raise ValueError("family Issue failure stage is invalid")
        self.stage = stage
        RuntimeError.__init__(self, "family Issue send outcome is unknown")


FamilyIssueTransport = Callable[
    [str, str, Mapping[str, str], bytes | None], HttpResponse
]


def _object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("family GitHub Issue JSON is invalid")
        result[key] = value
    return result


def _strict_json(body: bytes, *, ascii_only: bool) -> object:
    if not isinstance(body, bytes):
        raise ValueError("family GitHub Issue JSON is invalid")
    try:
        text = body.decode("ascii" if ascii_only else "utf-8")
        decoder = json.JSONDecoder(
            object_pairs_hook=_object_without_duplicates,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        payload, end = decoder.raw_decode(text)
        if end != len(text):
            raise ValueError()
        return payload
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        raise ValueError("family GitHub Issue JSON is invalid") from None


def _valid_authorization(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("Bearer "):
        return False
    token = value.removeprefix("Bearer ")
    return 16 <= len(token) <= 4096 and all(
        33 <= ord(character) <= 126 for character in token
    )


def _validate_headers(
    method: str, headers: Mapping[str, str]
) -> dict[str, str]:
    copied = dict(headers)
    expected_names = _POST_HEADER_NAMES if method == "POST" else _GET_HEADER_NAMES
    if (
        set(copied) != expected_names
        or copied.get("Accept") != "application/vnd.github+json"
        or not _valid_authorization(copied.get("Authorization"))
        or copied.get("X-GitHub-Api-Version") != GITHUB_API_VERSION
        or copied.get("User-Agent") != _USER_AGENT
        or (
            method == "POST"
            and copied.get("Content-Type") != "application/json"
        )
    ):
        raise ValueError("family GitHub Issue request is invalid")
    return copied


def _validate_transport_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
) -> tuple[str, dict[str, str]]:
    create_match = _CREATE_ENDPOINT.fullmatch(url)
    issue_match = _ISSUE_ENDPOINT.fullmatch(url)
    if method == "POST" and create_match is not None:
        if body is None or len(body) > _MAX_ISSUE_REQUEST_BYTES:
            raise ValueError("family GitHub Issue request is invalid")
        payload = _strict_json(body, ascii_only=True)
        if (
            type(payload) is not dict
            or set(payload) != {"title", "body"}
            or type(payload["title"]) is not str
            or type(payload["body"]) is not str
        ):
            raise ValueError("family GitHub Issue request is invalid")
    elif method == "GET" and issue_match is not None:
        if body is not None:
            raise ValueError("family GitHub Issue request is invalid")
        number = validate_issue_number(int(issue_match.group(3)))
        if issue_match.group(3) != str(number):
            raise ValueError("family GitHub Issue endpoint is not allowed")
    else:
        raise ValueError("family GitHub Issue endpoint is not allowed")
    return url.removeprefix(_API_ORIGIN), _validate_headers(method, headers)


def _fixed_reconciliation_failure() -> RuntimeError:
    return RuntimeError("family Issue reconciliation failed")


def family_issue_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
) -> HttpResponse:
    """Perform one fixed-origin request, exposing the actual body-send boundary."""

    path, checked_headers = _validate_transport_request(method, url, headers, body)
    connection: http.client.HTTPSConnection | None = None
    response: http.client.HTTPResponse | None = None
    body_send_begun = False
    response_phase = False
    try:
        connection = http.client.HTTPSConnection(_API_HOST, timeout=30)
        connection.connect()
        connection.putrequest(
            method,
            path,
            skip_host=True,
            skip_accept_encoding=True,
        )
        connection.putheader("Host", _API_HOST)
        for name, value in checked_headers.items():
            connection.putheader(name, value)
        if body is not None:
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders()
        if body is not None:
            body_send_begun = True
            connection.send(body)
        response_phase = True
        response = connection.getresponse()
        response_body = response.read(MAX_ISSUE_RESPONSE_BYTES + 1)
        if len(response_body) > MAX_ISSUE_RESPONSE_BYTES:
            if method == "POST":
                raise SendOutcomeUnknown("response")
            raise _fixed_reconciliation_failure()
        return HttpResponse(
            status=response.status,
            headers=dict(response.getheaders()),
            body=response_body,
        )
    except (SendNotStarted, SendOutcomeUnknown):
        raise
    except _KNOWN_TRANSPORT_ERRORS:
        if method == "POST":
            if not body_send_begun:
                raise SendNotStarted("send") from None
            stage = "response" if response_phase else "send"
            raise SendOutcomeUnknown(stage) from None
        raise _fixed_reconciliation_failure() from None
    finally:
        if response is not None:
            try:
                response.close()
            except _KNOWN_TRANSPORT_ERRORS:
                pass
        if connection is not None:
            try:
                connection.close()
            except _KNOWN_TRANSPORT_ERRORS:
                pass


def _validate_binding(binding: FamilyBinding) -> FamilyBinding:
    if type(binding) is not FamilyBinding or type(binding.repository) is not Repository:
        raise ValueError("family binding is invalid")
    try:
        repository = Repository.parse(binding.repository.slug)
        repository_id = validate_repository_id(binding.repository_id)
    except (TypeError, ValueError):
        raise ValueError("family binding is invalid") from None
    if repository != binding.repository or repository.slug != repository.slug.lower():
        raise ValueError("family binding is invalid")
    return FamilyBinding(repository, repository_id)


def _validate_canonical(canonical: CanonicalFamilyIssue) -> CanonicalFamilyIssue:
    if type(canonical) is not CanonicalFamilyIssue:
        raise ValueError("canonical family Issue is invalid")
    title = canonical.title
    body = canonical.body
    if type(title) is not str or type(body) is not str or not title or not body:
        raise ValueError("canonical family Issue is invalid")
    try:
        if (
            len(title.encode("utf-8")) > 256
            or len(body.encode("utf-8")) > _MAX_CANONICAL_BODY_BYTES
        ):
            raise ValueError("canonical family Issue is invalid")
    except UnicodeEncodeError:
        raise ValueError("canonical family Issue is invalid") from None
    return CanonicalFamilyIssue(title, body)


def _installation_token(
    tokens: FamilyInstallationTokenProvider,
) -> InstallationToken:
    try:
        token = tokens.get()
        if (
            type(token) is not InstallationToken
            or type(token.token) is not str
            or not 16 <= len(token.token) <= 4096
            or any(
                ord(character) < 33 or ord(character) > 126
                for character in token.token
            )
            or isinstance(token.expires_at, bool)
            or not isinstance(token.expires_at, int)
            or token.expires_at <= 0
        ):
            raise ValueError()
        return token
    except (ValueError, RuntimeError, OSError, http.client.HTTPException):
        raise SendNotStarted("token") from None


def _content_type(headers: Mapping[str, str]) -> str:
    if not isinstance(headers, Mapping):
        raise ValueError("family GitHub Issue response is invalid")
    values: list[object] = []
    for name, value in headers.items():
        if type(name) is not str:
            raise ValueError("family GitHub Issue response is invalid")
        if name.lower() == "content-type":
            values.append(value)
    if len(values) != 1 or not isinstance(values[0], str):
        raise ValueError("family GitHub Issue response is invalid")
    return values[0].split(";", 1)[0].strip()


def _parse_issue(
    response: HttpResponse,
    binding: FamilyBinding,
    *,
    expected_status: int,
    expected_number: int | None = None,
    canonical: CanonicalFamilyIssue | None = None,
) -> CreatedIssue:
    if type(response) is not HttpResponse:
        raise ValueError("family GitHub Issue response is invalid")
    if (
        isinstance(response.status, bool)
        or not isinstance(response.status, int)
        or response.status != expected_status
        or len(response.body) > MAX_ISSUE_RESPONSE_BYTES
    ):
        raise ValueError("family GitHub Issue response is invalid")
    if _content_type(response.headers) != "application/json":
        raise ValueError("family GitHub Issue response is invalid")
    payload = _strict_json(response.body, ascii_only=False)
    if type(payload) is not dict:
        raise ValueError("family GitHub Issue response is invalid")
    try:
        number = validate_issue_number(payload.get("number"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("family GitHub Issue response is invalid") from None
    url = f"https://github.com/{binding.repository.slug}/issues/{number}"
    repository_url = f"{_API_ORIGIN}/repos/{binding.repository.slug}"
    if (
        (expected_number is not None and number != expected_number)
        or payload.get("state") != "open"
        or type(payload.get("html_url")) is not str
        or payload["html_url"] != url
        or type(payload.get("repository_url")) is not str
        or payload["repository_url"] != repository_url
        or (
            canonical is not None
            and (
                type(payload.get("title")) is not str
                or payload["title"] != canonical.title
                or type(payload.get("body")) is not str
                or payload["body"] != canonical.body
            )
        )
    ):
        raise ValueError("family GitHub Issue response is invalid")
    return CreatedIssue(number, url)


def _headers(token: InstallationToken, *, content: bool) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token.token}",
    }
    if content:
        headers["Content-Type"] = "application/json"
    headers.update(
        {
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": _USER_AGENT,
        }
    )
    return headers


@dataclass
class FamilyIssueCreator:
    transport: FamilyIssueTransport = field(
        default=family_issue_transport,
        repr=False,
    )

    def create(
        self,
        binding: FamilyBinding,
        canonical: CanonicalFamilyIssue,
        tokens: FamilyInstallationTokenProvider,
    ) -> CreatedIssue:
        binding = _validate_binding(binding)
        canonical = _validate_canonical(canonical)
        token = _installation_token(tokens)
        body = json.dumps(
            {"title": canonical.title, "body": canonical.body},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        try:
            response = self.transport(
                "POST",
                f"{_API_ORIGIN}/repos/{binding.repository.slug}/issues",
                _headers(token, content=True),
                body,
            )
        except (SendNotStarted, SendOutcomeUnknown):
            raise
        except _KNOWN_TRANSPORT_ERRORS:
            raise SendOutcomeUnknown("send") from None
        try:
            return _parse_issue(response, binding, expected_status=201)
        except (TypeError, ValueError, RuntimeError):
            raise SendOutcomeUnknown("response") from None

    def verify_existing(
        self,
        binding: FamilyBinding,
        canonical: CanonicalFamilyIssue,
        issue_number: int,
        tokens: FamilyInstallationTokenProvider,
    ) -> CreatedIssue:
        binding = _validate_binding(binding)
        canonical = _validate_canonical(canonical)
        issue_number = validate_issue_number(issue_number)
        try:
            token = _installation_token(tokens)
            response = self.transport(
                "GET",
                (
                    f"{_API_ORIGIN}/repos/{binding.repository.slug}/issues/"
                    f"{issue_number}"
                ),
                _headers(token, content=False),
                None,
            )
            return _parse_issue(
                response,
                binding,
                expected_status=200,
                expected_number=issue_number,
                canonical=canonical,
            )
        except (
            SendNotStarted,
            SendOutcomeUnknown,
            TypeError,
            ValueError,
            RuntimeError,
            OSError,
            http.client.HTTPException,
        ):
            raise _fixed_reconciliation_failure() from None
