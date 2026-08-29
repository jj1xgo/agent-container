from datetime import datetime, timezone
import http.client
import json
import unittest
from unittest import mock

from agent_container.github_app import HttpResponse
from agent_container.github_app import InstallationToken
from agent_container.github_broker_error import BrokerStageError
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_issue import GitHubIssueTransport
from agent_container.github_issue import MAX_ISSUE_RESPONSE_BYTES
from agent_container.github_issue import github_issue_transport


def issue_payload(base_number: int, **changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": 9000 + base_number,
        "node_id": f"I_kwDO{base_number}",
        "number": base_number,
        "title": f"Issue {base_number}",
        "state": "open",
        "user": {
            "login": "octocat",
            "id": 1,
            "type": "User",
        },
        "labels": [],
        "body": "Issue body",
        "created_at": "2026-08-28T00:00:00Z",
        "updated_at": "2026-08-28T01:00:00.123Z",
        "html_url": (
            "https://github.com/jj1xgo/agent-container/issues/"
            f"{base_number}"
        ),
        "comments": 4,
        "locked": False,
    }
    payload.update(changes)
    return payload


def issue_summary(
    number: int, title: str, *, labels: list[str] | None = None
) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "state": "open",
        "author": "octocat",
        "labels": [] if labels is None else labels,
        "created_at": "2026-08-28T00:00:00Z",
        "updated_at": "2026-08-28T01:00:00.123Z",
        "url": f"https://github.com/jj1xgo/agent-container/issues/{number}",
    }


def json_response(payload: object, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status,
        {"Content-Type": "application/json; charset=utf-8"},
        json.dumps(payload).encode("utf-8"),
    )


class FakeTokens:
    def __init__(self) -> None:
        self.invalidations = 0

    def get(self) -> InstallationToken:
        return InstallationToken(
            "secret-installation-token",
            int(datetime(2026, 8, 28, 14, tzinfo=timezone.utc).timestamp()),
        )

    def invalidate(self) -> None:
        self.invalidations += 1


class FailingTokens(FakeTokens):
    def get(self) -> InstallationToken:
        raise RuntimeError("secret-token-marker")


class WrongStageTokens(FailingTokens):
    def get(self) -> InstallationToken:
        raise BrokerStageError("issue-request")


class FailingInvalidationTokens(FakeTokens):
    def invalidate(self) -> None:
        raise RuntimeError("secret-invalidation-marker")


def subject(
    *responses: HttpResponse,
) -> tuple[GitHubIssueTransport, list[tuple[object, ...]], FakeTokens]:
    queued = list(responses)
    calls: list[tuple[object, ...]] = []
    policy = BrokerPolicy.create(
        project_id="agent-container",
        repository="jj1xgo/agent-container",
        default_branch="main",
        protected_branches=("main",),
    )

    def transport(method, url, headers, body):  # type: ignore[no-untyped-def]
        calls.append((method, url, dict(headers), body))
        return queued.pop(0)

    tokens = FakeTokens()
    return GitHubIssueTransport(policy, tokens, transport), calls, tokens


class GitHubIssueTransportTest(unittest.TestCase):
    def assert_issue_error_safe(
        self, transport: GitHubIssueTransport, marker: str = "secret-marker"
    ) -> BrokerStageError:
        try:
            transport.view(12)
        except BrokerStageError as error:
            self.assertEqual(error.stage, "issue-request")
            self.assertNotIn(marker, str(error))
            self.assertNotIn(marker, repr(error))
            return error
        except Exception as error:  # noqa: BLE001
            self.fail(f"schema failure escaped as {type(error).__name__}")
        self.fail("BrokerStageError not raised")

    def test_lists_open_issues_in_response_order_and_excludes_pull_requests(
        self,
    ) -> None:
        response = json_response(
            [
                issue_payload(3, title="Newest"),
                issue_payload(
                    2, title="PR", pull_request={"url": "ignored"}
                ),
                issue_payload(
                    1,
                    title="Oldest",
                    labels=[{"name": "bug", "color": "f00"}],
                ),
            ]
        )
        transport, calls, _ = subject(response)

        self.assertEqual(
            transport.list_open(),
            {
                "issues": [
                    issue_summary(3, "Newest"),
                    issue_summary(1, "Oldest", labels=["bug"]),
                ]
            },
        )
        self.assertEqual(calls[0][0], "GET")
        self.assertTrue(
            str(calls[0][1]).endswith(
                "/issues?state=open&per_page=30&sort=created&direction=desc"
            )
        )
        self.assertIsNone(calls[0][3])

    def test_views_issue_with_body_and_normalizes_null_body(self) -> None:
        transport, calls, _ = subject(
            json_response(issue_payload(12, body=None))
        )

        result = transport.view(12)

        self.assertEqual(result["body"], "")
        self.assertEqual(
            set(result),
            {
                "number",
                "title",
                "state",
                "author",
                "labels",
                "body",
                "created_at",
                "updated_at",
                "url",
            },
        )
        self.assertEqual(
            calls[0][1],
            "https://api.github.com/repos/jj1xgo/agent-container/issues/12",
        )

    def test_accepts_schema_boundaries_and_deleted_author(self) -> None:
        title = "é" * 128
        label = "é" * 50
        body = "b" * (256 * 1024)
        transport, _, _ = subject(
            json_response(
                issue_payload(
                    12,
                    title=title,
                    state="closed",
                    user=None,
                    labels=[{"name": label}],
                    body=body,
                )
            )
        )

        result = transport.view(12)

        self.assertEqual(result["title"], title)
        self.assertEqual(result["state"], "closed")
        self.assertIsNone(result["author"])
        self.assertEqual(result["labels"], [label])
        self.assertEqual(result["body"], body)

    def test_requires_user_and_body_keys_even_when_null_is_allowed(self) -> None:
        for missing_key in ("user", "body"):
            with self.subTest(missing_key=missing_key):
                payload = issue_payload(12)
                del payload[missing_key]
                transport, _, _ = subject(json_response(payload))

                self.assert_issue_error_safe(transport)

    def test_list_rejects_closed_issue_from_open_only_endpoint(self) -> None:
        transport, _, _ = subject(
            json_response([issue_payload(12, state="closed")])
        )

        with self.assertRaises(BrokerStageError) as raised:
            transport.list_open()

        self.assertEqual(raised.exception.stage, "issue-request")

    def test_view_rejects_response_for_a_different_issue_number(self) -> None:
        transport, _, _ = subject(json_response(issue_payload(13)))

        self.assert_issue_error_safe(transport)

    def test_rejects_bounded_deep_json_without_recursion_error_escape(self) -> None:
        depth = 100_000
        response = HttpResponse(
            200,
            {"Content-Type": "application/json"},
            b"[" * depth + b"0" + b"]" * depth,
        )
        self.assertLess(len(response.body), MAX_ISSUE_RESPONSE_BYTES)
        transport, _, _ = subject(response)

        self.assert_issue_error_safe(transport)

    def test_rejects_invalid_issue_schema_without_partial_output(self) -> None:
        invalid_changes = (
            {"number": True},
            {"number": 0},
            {"title": ""},
            {"title": "x" * 257},
            {"state": "merged"},
            {"state": []},
            {"user": {}},
            {"labels": [{}]},
            {"created_at": "2026-08-28"},
            {
                "html_url": "https://github.com/other/repo/issues/12"
            },
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes):
                transport, _, _ = subject(
                    json_response(issue_payload(12, **changes))
                )
                self.assert_issue_error_safe(transport)

    def test_rejects_invalid_text_controls_and_timestamp_values(self) -> None:
        invalid_changes = (
            {"title": "bad\nheading"},
            {"title": "bad\x00heading"},
            {"body": "bad\x00body"},
            {"labels": [{"name": "bad\x7flabel"}]},
            {"updated_at": "2026-02-30T01:00:00Z"},
            {"updated_at": "2026-08-28T01:00:00+00:00"},
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes):
                transport, _, _ = subject(
                    json_response(issue_payload(12, **changes))
                )
                self.assert_issue_error_safe(transport)

    def test_rejects_list_and_label_count_bounds(self) -> None:
        responses = (
            json_response([issue_payload(number) for number in range(1, 32)]),
            json_response(
                issue_payload(
                    12,
                    labels=[{"name": "label"}] * 101,
                )
            ),
            json_response(
                issue_payload(
                    12,
                    labels=[{"name": "é" * 50 + "a"}],
                )
            ),
        )
        for response in responses:
            with self.subTest(body_length=len(response.body)):
                transport, _, _ = subject(response)
                with self.assertRaises(BrokerStageError) as raised:
                    if response is responses[0]:
                        transport.list_open()
                    else:
                        transport.view(12)
                self.assertEqual(raised.exception.stage, "issue-request")

    def test_accepts_body_at_limit_and_rejects_one_byte_over(self) -> None:
        accepted, _, _ = subject(
            json_response(issue_payload(12, body="b" * (256 * 1024)))
        )
        self.assertEqual(len(accepted.view(12)["body"]), 256 * 1024)

        rejected, _, _ = subject(
            json_response(issue_payload(12, body="b" * (256 * 1024 + 1)))
        )
        self.assert_issue_error_safe(rejected)

    def test_rejects_non_json_wrong_content_type_redirect_and_oversize(self) -> None:
        oversized = issue_payload(12, ignored="x" * MAX_ISSUE_RESPONSE_BYTES)
        responses = (
            HttpResponse(
                200,
                {"Content-Type": "application/json"},
                b'{"secret-marker"',
            ),
            HttpResponse(
                200,
                {"Content-Type": "text/plain"},
                json.dumps(
                    issue_payload(12, ignored="secret-marker")
                ).encode("utf-8"),
            ),
            HttpResponse(
                302,
                {
                    "Content-Type": "application/json",
                    "Location": "https://secret-marker.example/redirect",
                },
                json.dumps(issue_payload(12)).encode("utf-8"),
            ),
            json_response(oversized),
        )
        self.assertGreater(len(responses[-1].body), MAX_ISSUE_RESPONSE_BYTES)
        for response in responses:
            with self.subTest(status=response.status, length=len(response.body)):
                transport, _, _ = subject(response)
                self.assert_issue_error_safe(transport)

    def test_retries_one_unauthorized_response(self) -> None:
        transport, calls, tokens = subject(
            json_response({"message": "secret-marker"}, 401),
            json_response(issue_payload(12)),
        )

        try:
            result = transport.view(12)
        except BrokerStageError:
            self.fail("401 response was not retried")
        self.assertEqual(result["number"], 12)
        self.assertEqual(tokens.invalidations, 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], calls[1][1])

    def test_does_not_retry_non_unauthorized_or_second_unauthorized(self) -> None:
        for statuses, expected_calls, expected_invalidations in (
            ((403,), 1, 0),
            ((500,), 1, 0),
            ((401, 401), 2, 1),
        ):
            with self.subTest(statuses=statuses):
                transport, calls, tokens = subject(
                    *(json_response({"message": "secret-marker"}, status) for status in statuses)
                )
                self.assert_issue_error_safe(transport)
                self.assertEqual(len(calls), expected_calls)
                self.assertEqual(tokens.invalidations, expected_invalidations)

    def test_classifies_token_failures_without_secrets(self) -> None:
        transport, calls, _ = subject(json_response(issue_payload(12)))
        for tokens in (FailingTokens(), WrongStageTokens()):
            with self.subTest(tokens=type(tokens).__name__):
                transport.tokens = tokens  # type: ignore[assignment]
                with self.assertRaises(BrokerStageError) as raised:
                    transport.view(12)
                self.assertEqual(raised.exception.stage, "token")
                self.assertNotIn("secret-token-marker", repr(raised.exception))
        self.assertEqual(calls, [])

    def test_classifies_token_invalidation_failure_without_secret(self) -> None:
        transport, calls, _ = subject(
            json_response({"message": "secret-marker"}, 401)
        )
        transport.tokens = FailingInvalidationTokens()  # type: ignore[assignment]

        with self.assertRaises(BrokerStageError) as raised:
            transport.view(12)

        self.assertEqual(raised.exception.stage, "token")
        self.assertNotIn("secret-invalidation-marker", repr(raised.exception))
        self.assertEqual(len(calls), 1)

    def test_classifies_transport_failure_without_secret(self) -> None:
        transport, _, _ = subject()

        def failing_transport(method, url, headers, body):  # type: ignore[no-untyped-def]
            raise RuntimeError("secret-transport-marker")

        transport.transport = failing_transport

        self.assert_issue_error_safe(transport, "secret-transport-marker")

    def test_rejects_invalid_view_number_before_request(self) -> None:
        transport, calls, _ = subject()
        for number in (True, 0, -1, 2_147_483_648):
            with self.subTest(number=number), self.assertRaises(ValueError):
                transport.view(number)  # type: ignore[arg-type]
        self.assertEqual(calls, [])


class GitHubIssueHttpTransportTest(unittest.TestCase):
    def test_allows_only_bodyless_get_to_fixed_issue_endpoints(self) -> None:
        valid_url = (
            "https://api.github.com/repos/jj1xgo/agent-container/issues/12"
        )
        invalid_requests = (
            ("POST", valid_url, None),
            ("GET", valid_url, b"{}"),
            (
                "GET",
                "https://api.github.com/repos/jj1xgo/agent-container/issues?state=all",
                None,
            ),
            (
                "GET",
                "https://api.github.com/repos/other/repo/pulls/12",
                None,
            ),
        )
        for method, url, body in invalid_requests:
            with self.subTest(method=method, url=url, body=body):
                with self.assertRaises(ValueError):
                    github_issue_transport(method, url, {}, body)

    def test_reads_only_one_byte_past_limit_and_closes_response(self) -> None:
        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            def __init__(self) -> None:
                self.maximum: int | None = None
                self.closed = False

            def read(self, maximum: int) -> bytes:
                self.maximum = maximum
                return b"x" * maximum

            def close(self) -> None:
                self.closed = True

        class FakeOpener:
            def __init__(self, response: FakeResponse) -> None:
                self.response = response

            def open(self, request, timeout):  # type: ignore[no-untyped-def]
                return self.response

        response = FakeResponse()
        with mock.patch(
            "agent_container.github_issue.urllib.request.build_opener",
            return_value=FakeOpener(response),
        ):
            with self.assertRaisesRegex(RuntimeError, "too large"):
                github_issue_transport(
                    "GET",
                    "https://api.github.com/repos/jj1xgo/agent-container/issues/12",
                    {},
                    None,
                )

        self.assertEqual(response.maximum, MAX_ISSUE_RESPONSE_BYTES + 1)
        self.assertTrue(response.closed)

    def test_sanitizes_protocol_errors_from_open_read_and_close(self) -> None:
        marker = "secret-malformed-status"

        class ProtocolResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            def __init__(self, phase: str) -> None:
                self.phase = phase

            def read(self, maximum: int) -> bytes:
                if self.phase == "read":
                    raise http.client.BadStatusLine(marker)
                return b"{}"

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

        for phase in ("open", "read", "close"):
            with self.subTest(phase=phase):
                with mock.patch(
                    "agent_container.github_issue.urllib.request.build_opener",
                    return_value=ProtocolOpener(phase),
                ):
                    try:
                        github_issue_transport(
                            "GET",
                            "https://api.github.com/repos/"
                            "jj1xgo/agent-container/issues/12",
                            {},
                            None,
                        )
                    except RuntimeError as error:
                        self.assertEqual(str(error), "GitHub Issue request failed")
                        self.assertNotIn(marker, str(error))
                        self.assertNotIn(marker, repr(error))
                        self.assertIsNone(error.__cause__)
                    except Exception as error:  # noqa: BLE001
                        self.fail(
                            "protocol failure escaped as "
                            f"{type(error).__name__}: {error}"
                        )
                    else:
                        self.fail("RuntimeError not raised")


if __name__ == "__main__":
    unittest.main()
