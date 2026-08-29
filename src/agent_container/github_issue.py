from dataclasses import dataclass
from datetime import datetime
import http.client
import json
import re
from typing import Callable, Mapping
import urllib.error
import urllib.request

from agent_container.github_app import HttpResponse
from agent_container.github_app import InstallationToken
from agent_container.github_app import InstallationTokenProvider
from agent_container.github_broker_error import BrokerStageError
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_broker_policy import validate_issue_number


GITHUB_API = "https://api.github.com"
MAX_ISSUE_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_TITLE_BYTES = 256
_MAX_BODY_BYTES = 256 * 1024
_MAX_LABEL_BYTES = 100
_MAX_LABELS = 100
_MAX_LIST_ITEMS = 30
_LIST_PATH = "/issues?state=open&per_page=30&sort=created&direction=desc"
_ISSUE_ENDPOINT = re.compile(
    r"^https://api\.github\.com/repos/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/issues"
    r"(?:/[1-9][0-9]*|\?state=open&per_page=30&sort=created&direction=desc)$"
)
_UTC_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$"
)
IssueRestTransport = Callable[
    [str, str, Mapping[str, str], bytes | None], HttpResponse
]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def github_issue_transport(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> HttpResponse:
    if method != "GET" or body is not None or _ISSUE_ENDPOINT.fullmatch(url) is None:
        raise ValueError("GitHub Issue endpoint is not allowed")
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        try:
            response = urllib.request.build_opener(_NoRedirect()).open(
                request, timeout=30
            )
        except urllib.error.HTTPError as error:
            response = error
        try:
            response_body = response.read(MAX_ISSUE_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_ISSUE_RESPONSE_BYTES:
                raise RuntimeError("GitHub Issue response is too large")
            return HttpResponse(
                response.status, dict(response.headers.items()), response_body
            )
        finally:
            response.close()
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        http.client.HTTPException,
    ):
        raise RuntimeError("GitHub Issue request failed") from None


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


def _object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("GitHub Issue response is invalid")
        result[key] = value
    return result


def _json(response: HttpResponse, expected: int) -> object:
    if len(response.body) > MAX_ISSUE_RESPONSE_BYTES or response.status != expected:
        raise RuntimeError("GitHub Issue request failed")
    if (
        response.headers.get("Content-Type", "").split(";", 1)[0].strip()
        != "application/json"
    ):
        raise ValueError("GitHub Issue response is invalid")
    try:
        return json.loads(
            response.body.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("GitHub Issue response is invalid")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise ValueError("GitHub Issue response is invalid") from None


def _text(
    value: object,
    *,
    maximum_bytes: int,
    allow_empty: bool,
    allow_newline: bool,
) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ValueError("GitHub Issue response is invalid")
    try:
        if len(value.encode("utf-8")) > maximum_bytes:
            raise ValueError("GitHub Issue response is invalid")
    except UnicodeEncodeError:
        raise ValueError("GitHub Issue response is invalid") from None
    for character in value:
        codepoint = ord(character)
        if codepoint == 127 or (
            codepoint < 32
            and not (allow_newline and character in {"\n", "\t"})
        ):
            raise ValueError("GitHub Issue response is invalid")
    return value


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise ValueError("GitHub Issue response is invalid")
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        raise ValueError("GitHub Issue response is invalid") from None
    return value


def _author(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("GitHub Issue response is invalid")
    return _text(
        value.get("login"),
        maximum_bytes=MAX_ISSUE_RESPONSE_BYTES,
        allow_empty=False,
        allow_newline=False,
    )


def _labels(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > _MAX_LABELS:
        raise ValueError("GitHub Issue response is invalid")
    labels: list[str] = []
    for label in value:
        if not isinstance(label, dict):
            raise ValueError("GitHub Issue response is invalid")
        labels.append(
            _text(
                label.get("name"),
                maximum_bytes=_MAX_LABEL_BYTES,
                allow_empty=False,
                allow_newline=False,
            )
        )
    return labels


def _summary(
    policy: BrokerPolicy, payload: dict[str, object]
) -> dict[str, object]:
    if "user" not in payload:
        raise ValueError("GitHub Issue response is invalid")
    number = validate_issue_number(payload.get("number"))  # type: ignore[arg-type]
    title = _text(
        payload.get("title"),
        maximum_bytes=_MAX_TITLE_BYTES,
        allow_empty=False,
        allow_newline=False,
    )
    state = payload.get("state")
    created_at = _timestamp(payload.get("created_at"))
    updated_at = _timestamp(payload.get("updated_at"))
    html_url = payload.get("html_url")
    if (
        not isinstance(state, str)
        or state not in {"open", "closed"}
        or not isinstance(html_url, str)
        or html_url
        != f"https://github.com/{policy.repository.slug}/issues/{number}"
    ):
        raise ValueError("GitHub Issue response is invalid")
    return {
        "number": number,
        "title": title,
        "state": state,
        "author": _author(payload["user"]),
        "labels": _labels(payload.get("labels")),
        "created_at": created_at,
        "updated_at": updated_at,
        "url": html_url,
    }


@dataclass
class GitHubIssueTransport:
    policy: BrokerPolicy
    tokens: InstallationTokenProvider
    transport: IssueRestTransport = github_issue_transport

    def _request(self, path: str) -> HttpResponse:
        url = f"{GITHUB_API}/repos/{self.policy.repository.slug}{path}"
        for attempt in range(2):
            token = _get_installation_token(self.tokens)
            try:
                response = self.transport(
                    "GET",
                    url,
                    {
                        "Accept": "application/vnd.github+json",
                        "Authorization": f"Bearer {token.token}",
                        "Content-Type": "application/json",
                        "X-GitHub-Api-Version": "2026-03-10",
                        "User-Agent": "agent-container-github-broker",
                    },
                    None,
                )
            except (
                ValueError,
                RuntimeError,
                OSError,
                http.client.HTTPException,
            ):
                raise BrokerStageError("issue-request") from None
            if response.status != 401 or attempt == 1:
                return response
            _invalidate_installation_token(self.tokens)
        raise AssertionError("unreachable")

    def list_open(self) -> dict[str, object]:
        try:
            payload = _json(self._request(_LIST_PATH), 200)
            if not isinstance(payload, list) or len(payload) > _MAX_LIST_ITEMS:
                raise ValueError("GitHub Issue response is invalid")
            issues = []
            for item in payload:
                if not isinstance(item, dict):
                    raise ValueError("GitHub Issue response is invalid")
                if "pull_request" not in item:
                    summary = _summary(self.policy, item)
                    if summary["state"] != "open":
                        raise ValueError("GitHub Issue response is invalid")
                    issues.append(summary)
            return {"issues": issues}
        except BrokerStageError:
            raise
        except (ValueError, RuntimeError):
            raise BrokerStageError("issue-request") from None

    def view(self, number: int) -> dict[str, object]:
        number = validate_issue_number(number)
        try:
            payload = _json(self._request(f"/issues/{number}"), 200)
            if not isinstance(payload, dict):
                raise ValueError("GitHub Issue response is invalid")
            summary = _summary(self.policy, payload)
            if summary["number"] != number or "body" not in payload:
                raise ValueError("GitHub Issue response is invalid")
            body = payload["body"]
            if body is None:
                body = ""
            body = _text(
                body,
                maximum_bytes=_MAX_BODY_BYTES,
                allow_empty=True,
                allow_newline=True,
            )
            return {**summary, "body": body}
        except BrokerStageError:
            raise
        except (ValueError, RuntimeError):
            raise BrokerStageError("issue-request") from None
