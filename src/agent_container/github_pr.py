from dataclasses import dataclass
import json
from typing import Callable, Mapping
import urllib.error
import urllib.request

from agent_container.github_app import HttpResponse
from agent_container.github_app import InstallationToken
from agent_container.github_app import InstallationTokenProvider
from agent_container.github_broker_error import BrokerStageError
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_broker_policy import validate_pr_body
from agent_container.github_broker_policy import validate_pr_number
from agent_container.github_broker_policy import validate_pr_title


GITHUB_API = "https://api.github.com"
MAX_PR_RESPONSE_BYTES = 2 * 1024 * 1024
RestTransport = Callable[[str, str, Mapping[str, str], bytes | None], HttpResponse]


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


def _validated_json(response: HttpResponse, expected: int) -> dict[str, object]:
    try:
        return _json_object(response, expected)
    except (ValueError, RuntimeError):
        raise BrokerStageError("pr-request") from None


def _validated_summary(payload: dict[str, object]) -> dict[str, object]:
    try:
        return GitHubPullRequestTransport._summary(payload)
    except ValueError:
        raise BrokerStageError("pr-request") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def github_rest_transport(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> HttpResponse:
    if method not in {"GET", "POST"} or not url.startswith(f"{GITHUB_API}/repos/"):
        raise ValueError("GitHub pull request endpoint is not allowed")
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        response = urllib.request.build_opener(_NoRedirect()).open(request, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError("GitHub pull request request failed") from None
    try:
        response_body = response.read(MAX_PR_RESPONSE_BYTES + 1)
        if len(response_body) > MAX_PR_RESPONSE_BYTES:
            raise RuntimeError("GitHub pull request response is too large")
        return HttpResponse(response.status, dict(response.headers.items()), response_body)
    finally:
        response.close()


def _json_object(response: HttpResponse, expected: int) -> dict[str, object]:
    if response.status != expected:
        raise RuntimeError(f"GitHub pull request request failed with HTTP {response.status}")
    if response.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
        raise ValueError("GitHub pull request response is invalid")
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("GitHub pull request response is invalid") from None
    if not isinstance(payload, dict):
        raise ValueError("GitHub pull request response is invalid")
    return payload


@dataclass
class GitHubPullRequestTransport:
    policy: BrokerPolicy
    tokens: InstallationTokenProvider
    transport: RestTransport = github_rest_transport

    def _request(self, method: str, path: str, body: bytes | None = None) -> HttpResponse:
        for attempt in range(2):
            token = _get_installation_token(self.tokens)
            try:
                response = self.transport(
                    method,
                    f"{GITHUB_API}/repos/{self.policy.repository.slug}{path}",
                    {
                        "Accept": "application/vnd.github+json",
                        "Authorization": f"Bearer {token.token}",
                        "Content-Type": "application/json",
                        "X-GitHub-Api-Version": "2026-03-10",
                        "User-Agent": "agent-container-github-broker",
                    },
                    body,
                )
            except (RuntimeError, OSError):
                raise BrokerStageError("pr-request") from None
            if response.status != 401 or attempt == 1:
                return response
            _invalidate_installation_token(self.tokens)
        raise AssertionError("unreachable")

    @staticmethod
    def _summary(payload: dict[str, object]) -> dict[str, object]:
        number = payload.get("number")
        state = payload.get("state")
        title = payload.get("title")
        html_url = payload.get("html_url")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or not isinstance(state, str)
            or not isinstance(title, str)
            or not isinstance(html_url, str)
            or not html_url.startswith("https://github.com/")
        ):
            raise ValueError("GitHub pull request response is invalid")
        validate_pr_number(number)
        validate_pr_title(title)
        return {"number": number, "state": state, "title": title, "url": html_url}

    def create(self, *, base: str, head: str, title: str, body: str) -> dict[str, object]:
        if base != self.policy.default_branch:
            raise ValueError("pull request base is not allowed")
        head = self.policy.validate_work_branch(head)
        title = validate_pr_title(title)
        body = validate_pr_body(body)
        request = json.dumps(
            {"base": base, "body": body, "head": head, "title": title},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return _validated_summary(
            _validated_json(self._request("POST", "/pulls", request), 201)
        )

    def view(self, number: int) -> dict[str, object]:
        number = validate_pr_number(number)
        return _validated_summary(
            _validated_json(self._request("GET", f"/pulls/{number}"), 200)
        )

    def checks(self, number: int) -> dict[str, object]:
        number = validate_pr_number(number)
        try:
            return self._checks(number)
        except BrokerStageError:
            raise
        except ValueError:
            raise BrokerStageError("pr-request") from None

    def _checks(self, number: int) -> dict[str, object]:
        pull = _validated_json(self._request("GET", f"/pulls/{number}"), 200)
        head = pull.get("head")
        sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(sha, str) or len(sha) not in {40, 64} or any(
            character not in "0123456789abcdef" for character in sha
        ):
            raise ValueError("GitHub pull request response is invalid")
        checks = _validated_json(
            self._request("GET", f"/commits/{sha}/check-runs"), 200
        )
        runs = checks.get("check_runs")
        if not isinstance(runs, list) or len(runs) > 100:
            raise ValueError("GitHub check response is invalid")
        output = []
        for run in runs:
            if not isinstance(run, dict):
                raise ValueError("GitHub check response is invalid")
            name, status, conclusion = (
                run.get("name"), run.get("status"), run.get("conclusion")
            )
            if not isinstance(name, str) or not isinstance(status, str) or not (
                conclusion is None or isinstance(conclusion, str)
            ):
                raise ValueError("GitHub check response is invalid")
            output.append({"name": name, "status": status, "conclusion": conclusion})
        return {"number": number, "checks": output}
