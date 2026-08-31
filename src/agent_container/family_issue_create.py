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


def _is_clean_domain_error(
    error: Exception,
    expected_type: type[SendNotStarted] | type[SendOutcomeUnknown],
    expected_stage: str,
) -> bool:
    return (
        type(error) is expected_type
        and error.stage == expected_stage  # type: ignore[attr-defined]
        and error.__cause__ is None
        and error.__context__ is None
    )


def _response_header_values(
    pairs: tuple[tuple[str, str], ...],
    expected_name: str,
) -> list[str]:
    values: list[str] = []
    for pair in pairs:
        if (
            type(pair) is not tuple
            or len(pair) != 2
            or type(pair[0]) is not str
            or type(pair[1]) is not str
        ):
            raise ValueError("family GitHub Issue response is invalid")
        if pair[0].lower() == expected_name:
            values.append(pair[1])
    return values


def _validated_response_header_pairs(
    pairs: tuple[tuple[str, str], ...],
) -> int | None:
    content_types = _response_header_values(pairs, "content-type")
    if (
        len(content_types) != 1
        or content_types[0].split(";", 1)[0].strip() != "application/json"
    ):
        raise ValueError("family GitHub Issue response is invalid")
    lengths = _response_header_values(pairs, "content-length")
    if not lengths:
        return None
    if len(lengths) != 1:
        raise ValueError("family GitHub Issue response is invalid")
    value = lengths[0]
    if (
        not value
        or len(value) > 10
        or not value.isascii()
        or not value.isdecimal()
    ):
        raise ValueError("family GitHub Issue response is invalid")
    length = int(value)
    if length > MAX_ISSUE_RESPONSE_BYTES:
        raise ValueError("family GitHub Issue response is invalid")
    return length


def _read_framed_response(
    response: http.client.HTTPResponse,
    declared_length: int | None,
) -> bytes:
    initial_length = response.length
    chunked = response.chunked
    if (
        type(chunked) is not bool
        or isinstance(initial_length, bool)
        or (initial_length is not None and not isinstance(initial_length, int))
        or (declared_length is not None and chunked)
        or (
            declared_length is not None
            and initial_length != declared_length
        )
        or (
            declared_length is None
            and not chunked
            and initial_length is not None
        )
    ):
        raise ValueError("family GitHub Issue response is invalid")

    remaining = MAX_ISSUE_RESPONSE_BYTES + 1
    chunks: list[bytes] = []
    while remaining:
        chunk = response.read(remaining)
        if type(chunk) is not bytes:
            raise ValueError("family GitHub Issue response is invalid")
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
        if response.isclosed():
            break
    body = b"".join(chunks)
    if len(body) > MAX_ISSUE_RESPONSE_BYTES:
        raise ValueError("family GitHub Issue response is invalid")

    final_length = response.length
    if (
        isinstance(final_length, bool)
        or (final_length is not None and not isinstance(final_length, int))
        or not response.isclosed()
        or (
            declared_length is not None
            and (len(body) != declared_length or final_length != 0)
        )
        or (
            declared_length is None
            and final_length is not None
        )
    ):
        raise ValueError("family GitHub Issue response is invalid")
    return body


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
    result: HttpResponse | None = None
    failure: Exception | None = None
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
        raw_headers = tuple(response.getheaders())
        declared_length = _validated_response_header_pairs(raw_headers)
        response_body = _read_framed_response(response, declared_length)
        result = HttpResponse(
            status=response.status,
            headers=dict(raw_headers),
            body=response_body,
        )
    except Exception as error:
        if method == "POST":
            if not body_send_begun:
                if _is_clean_domain_error(error, SendNotStarted, "send"):
                    failure = error
                else:
                    failure = SendNotStarted("send")
            else:
                stage = "response" if response_phase else "send"
                if _is_clean_domain_error(error, SendOutcomeUnknown, stage):
                    failure = error
                else:
                    failure = SendOutcomeUnknown(stage)
        else:
            failure = _fixed_reconciliation_failure()
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
    if failure is not None:
        raise failure
    if result is None:
        raise AssertionError("family GitHub Issue transport has no result")
    return result


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
    except Exception:
        pass
    else:
        return token
    raise SendNotStarted("token")


def _validated_response_headers(headers: Mapping[str, str]) -> int | None:
    if not isinstance(headers, Mapping):
        raise ValueError("family GitHub Issue response is invalid")
    pairs: list[tuple[str, str]] = []
    for name, value in headers.items():
        if type(name) is not str or type(value) is not str:
            raise ValueError("family GitHub Issue response is invalid")
        pairs.append((name, value))
    return _validated_response_header_pairs(tuple(pairs))


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
        or type(response.body) is not bytes
        or len(response.body) > MAX_ISSUE_RESPONSE_BYTES
    ):
        raise ValueError("family GitHub Issue response is invalid")
    declared_length = _validated_response_headers(response.headers)
    if declared_length is not None and declared_length != len(response.body):
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
        response: HttpResponse | None = None
        transport_failure: Exception | None = None
        try:
            response = self.transport(
                "POST",
                f"{_API_ORIGIN}/repos/{binding.repository.slug}/issues",
                _headers(token, content=True),
                body,
            )
        except Exception as error:
            if _is_clean_domain_error(error, SendNotStarted, "send"):
                transport_failure = error
            elif any(
                _is_clean_domain_error(error, SendOutcomeUnknown, stage)
                for stage in ("send", "response")
            ):
                transport_failure = error
            else:
                transport_failure = SendOutcomeUnknown("send")
        if transport_failure is not None:
            raise transport_failure
        if response is None:
            raise SendOutcomeUnknown("send")
        response_failure = False
        try:
            return _parse_issue(response, binding, expected_status=201)
        except Exception:
            response_failure = True
        if response_failure:
            raise SendOutcomeUnknown("response")
        raise AssertionError("unreachable")

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
        failure = False
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
        except Exception:
            failure = True
        if failure:
            raise _fixed_reconciliation_failure()
        raise AssertionError("unreachable")
