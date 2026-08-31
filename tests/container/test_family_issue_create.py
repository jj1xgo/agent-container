import http.client
from io import BytesIO
import json
import unittest
from unittest import mock

from agent_container.family_issue import CanonicalFamilyIssue
from agent_container.family_issue_create import CreatedIssue
from agent_container.family_issue_create import FamilyIssueCreator
from agent_container.family_issue_create import MAX_ISSUE_RESPONSE_BYTES
from agent_container.family_issue_create import SendNotStarted
from agent_container.family_issue_create import SendOutcomeUnknown
from agent_container.family_issue_create import family_issue_transport
from agent_container.family_state import FamilyBinding
from agent_container.github_app import HttpResponse
from agent_container.github_app import InstallationToken
from agent_container.github_values import MAX_ISSUE_NUMBER
from agent_container.state import Repository


API_VERSION = "2026-03-10"
CREATE_URL = "https://api.github.com/repos/family/roadmap/issues"
ISSUE_URL = "https://github.com/family/roadmap/issues/73"
REPOSITORY_URL = "https://api.github.com/repos/family/roadmap"
CANONICAL = CanonicalFamilyIssue(
    "Plan the family roadmap",
    "## Summary\n\nPlan the next release.\n\n"
    "## Context\n\nKeep the exact approved scope.\n\n"
    "## Acceptance criteria\n\n- Publish the plan\n",
)
EXPECTED_BODY = (
    b'{"title":"Plan the family roadmap","body":"## Summary\\n\\nPlan the next '
    b'release.\\n\\n## Context\\n\\nKeep the exact approved scope.\\n\\n## '
    b'Acceptance criteria\\n\\n- Publish the plan\\n"}'
)


def complete_issue_fixture(**changes: object) -> dict[str, object]:
    """Complete documented Issue response with hand-fixed family values."""

    fixture: dict[str, object] = {
        "url": "https://api.github.com/repos/family/roadmap/issues/73",
        "repository_url": REPOSITORY_URL,
        "labels_url": "https://api.github.com/repos/family/roadmap/issues/73/labels{/name}",
        "comments_url": "https://api.github.com/repos/family/roadmap/issues/73/comments",
        "events_url": "https://api.github.com/repos/family/roadmap/issues/73/events",
        "html_url": ISSUE_URL,
        "id": 900073,
        "node_id": "I_kwDOFamilyRoadmap73",
        "number": 73,
        "title": CANONICAL.title,
        "user": {
            "login": "family-maintainer",
            "id": 7,
            "node_id": "U_kgDOFamilyMaintainer",
            "avatar_url": "https://avatars.githubusercontent.com/u/7?v=4",
            "gravatar_id": "",
            "url": "https://api.github.com/users/family-maintainer",
            "html_url": "https://github.com/family-maintainer",
            "followers_url": "https://api.github.com/users/family-maintainer/followers",
            "following_url": "https://api.github.com/users/family-maintainer/following{/other_user}",
            "gists_url": "https://api.github.com/users/family-maintainer/gists{/gist_id}",
            "starred_url": "https://api.github.com/users/family-maintainer/starred{/owner}{/repo}",
            "subscriptions_url": "https://api.github.com/users/family-maintainer/subscriptions",
            "organizations_url": "https://api.github.com/users/family-maintainer/orgs",
            "repos_url": "https://api.github.com/users/family-maintainer/repos",
            "events_url": "https://api.github.com/users/family-maintainer/events{/privacy}",
            "received_events_url": "https://api.github.com/users/family-maintainer/received_events",
            "type": "User",
            "site_admin": False,
        },
        "labels": [],
        "state": "open",
        "locked": False,
        "assignee": None,
        "assignees": [],
        "milestone": None,
        "comments": 0,
        "created_at": "2026-08-31T10:00:00Z",
        "updated_at": "2026-08-31T10:00:00Z",
        "closed_at": None,
        "author_association": "OWNER",
        "active_lock_reason": None,
        "body": CANONICAL.body,
        "closed_by": None,
        "reactions": {
            "url": "https://api.github.com/repos/family/roadmap/issues/73/reactions",
            "total_count": 0,
            "+1": 0,
            "-1": 0,
            "laugh": 0,
            "hooray": 0,
            "confused": 0,
            "heart": 0,
            "rocket": 0,
            "eyes": 0,
        },
        "timeline_url": "https://api.github.com/repos/family/roadmap/issues/73/timeline",
        "performed_via_github_app": None,
        "state_reason": None,
    }
    fixture.update(changes)
    return fixture


def issue_response(
    *,
    payload: object | None = None,
    status: int = 201,
    content_type: str = "application/json; charset=utf-8",
) -> HttpResponse:
    selected = complete_issue_fixture() if payload is None else payload
    return HttpResponse(
        status,
        {"Content-Type": content_type},
        json.dumps(selected, ensure_ascii=True, separators=(",", ":")).encode(
            "ascii"
        ),
    )


def framed_issue_response() -> HttpResponse:
    body = issue_response().body
    return HttpResponse(
        201,
        {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
        },
        body,
    )


class FakeTokens:
    def __init__(self) -> None:
        self.get_calls = 0
        self.invalidate_calls = 0

    def get(self) -> InstallationToken:
        self.get_calls += 1
        return InstallationToken("family-installation-token-marker", 1_800_003_600)

    def invalidate(self) -> None:
        self.invalidate_calls += 1


class FailingTokens(FakeTokens):
    def get(self) -> InstallationToken:
        self.get_calls += 1
        raise RuntimeError("family-token-secret-marker")


class UnexpectedFailingTokens(FakeTokens):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def get(self) -> InstallationToken:
        self.get_calls += 1
        raise self.error


def creator_subject(
    *responses: HttpResponse,
) -> tuple[
    FamilyIssueCreator,
    list[tuple[str, str, dict[str, str], bytes | None]],
]:
    queued = list(responses)
    calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def transport(method, url, headers, body):  # type: ignore[no-untyped-def]
        calls.append((method, url, dict(headers), body))
        return queued.pop(0)

    return FamilyIssueCreator(transport=transport), calls


class FamilyIssueCreatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.binding = FamilyBinding(Repository("family", "roadmap"), 42)
        self.tokens = FakeTokens()

    # Break caught: create targeting another endpoint, adding fields, or retrying.
    def test_creates_once_with_exact_endpoint_headers_body_and_result(self) -> None:
        creator, calls = creator_subject(issue_response())

        created = creator.create(self.binding, CANONICAL, self.tokens)  # type: ignore[arg-type]

        self.assertEqual(created, CreatedIssue(73, ISSUE_URL))
        self.assertEqual(self.tokens.get_calls, 1)
        self.assertEqual(self.tokens.invalidate_calls, 0)
        self.assertEqual(
            calls,
            [
                (
                    "POST",
                    CREATE_URL,
                    {
                        "Accept": "application/vnd.github+json",
                        "Authorization": "Bearer family-installation-token-marker",
                        "Content-Type": "application/json",
                        "X-GitHub-Api-Version": API_VERSION,
                        "User-Agent": "agent-container-family-approval",
                    },
                    EXPECTED_BODY,
                )
            ],
        )

    # Break caught: 401 token refresh or HTTP/redirect response retrying a POST.
    def test_never_retries_401_redirect_or_other_http_status(self) -> None:
        for status in (301, 302, 307, 308, 400, 401, 403, 404, 422, 500, 503):
            with self.subTest(status=status):
                response = issue_response(status=status)
                creator, calls = creator_subject(response, issue_response())
                tokens = FakeTokens()

                with self.assertRaises(SendOutcomeUnknown) as raised:
                    creator.create(self.binding, CANONICAL, tokens)  # type: ignore[arg-type]

                self.assertEqual(raised.exception.stage, "response")
                self.assertEqual(len(calls), 1)
                self.assertEqual(tokens.get_calls, 1)
                self.assertEqual(tokens.invalidate_calls, 0)

    # Break caught: accepting a response that cannot prove the exact created Issue.
    def test_rejects_unproven_creation_responses_as_unknown_without_retry(self) -> None:
        duplicate_number = (
            b'{"number":73,"number":74,"state":"open",'
            b'"html_url":"https://github.com/family/roadmap/issues/73",'
            b'"repository_url":"https://api.github.com/repos/family/roadmap"}'
        )
        exact = complete_issue_fixture()
        response_cases = (
            HttpResponse(201, {"Content-Type": "text/plain"}, b"secret-marker"),
            HttpResponse(  # type: ignore[arg-type]
                201, None, issue_response().body
            ),
            HttpResponse(
                201,
                {"Content-Type": "application/json"},
                b"x" * (MAX_ISSUE_RESPONSE_BYTES + 1),
            ),
            HttpResponse(201, {"Content-Type": "application/json"}, b"{"),
            HttpResponse(
                201,
                {"Content-Type": "application/json"},
                issue_response().body + b"\n",
            ),
            HttpResponse(201, {"Content-Type": "application/json"}, duplicate_number),
            HttpResponse(
                201,
                {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(issue_response().body) + 1),
                },
                issue_response().body,
            ),
            HttpResponse(
                201,
                {
                    "Content-Type": "application/json",
                    "content-type": "application/json",
                },
                issue_response().body,
            ),
            HttpResponse(
                201,
                {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(issue_response().body)),
                    "content-length": str(len(issue_response().body)),
                },
                issue_response().body,
            ),
            issue_response(payload=[]),
            issue_response(payload={key: value for key, value in exact.items() if key != "state"}),
            issue_response(payload=exact | {"number": True}),
            issue_response(payload=exact | {"number": 0}),
            issue_response(payload=exact | {"number": MAX_ISSUE_NUMBER + 1}),
            issue_response(payload=exact | {"state": "closed"}),
            issue_response(payload=exact | {"state": "OPEN"}),
            issue_response(
                payload=exact
                | {"html_url": "https://github.com/other/roadmap/issues/73"}
            ),
            issue_response(
                payload=exact
                | {"html_url": "https://github.com/family/roadmap/issues/74"}
            ),
            issue_response(
                payload=exact
                | {"repository_url": "https://api.github.com/repos/other/roadmap"}
            ),
        )
        for response in response_cases:
            with self.subTest(status=response.status, size=len(response.body)):
                creator, calls = creator_subject(response, issue_response())

                with self.assertRaises(SendOutcomeUnknown) as raised:
                    creator.create(self.binding, CANONICAL, self.tokens)  # type: ignore[arg-type]

                self.assertEqual(raised.exception.stage, "response")
                self.assertEqual(len(calls), 1)
                self.assertNotIn("secret-marker", str(raised.exception))
                self.assertNotIn("secret-marker", repr(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertIsNone(raised.exception.__context__)

    # Break caught: a token failure being mistaken for an ambiguous Issue POST.
    def test_token_failure_is_proven_not_started_and_never_calls_transport(self) -> None:
        creator, calls = creator_subject(issue_response())
        tokens = FailingTokens()

        with self.assertRaises(SendNotStarted) as raised:
            creator.create(self.binding, CANONICAL, tokens)  # type: ignore[arg-type]

        self.assertEqual(raised.exception.stage, "token")
        self.assertEqual(tokens.get_calls, 1)
        self.assertEqual(calls, [])
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("secret", repr(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    # Break caught: an ordinary provider exception escaping before the Issue POST.
    def test_every_ordinary_token_failure_is_sanitized_before_http(self) -> None:
        for failure in (
            TypeError("token-type-secret-marker"),
            AttributeError("token-attribute-secret-marker"),
            LookupError("token-lookup-secret-marker"),
        ):
            with self.subTest(failure=type(failure).__name__):
                creator, calls = creator_subject(issue_response())
                tokens = UnexpectedFailingTokens(failure)

                with self.assertRaises(SendNotStarted) as raised:
                    creator.create(self.binding, CANONICAL, tokens)  # type: ignore[arg-type]

                self.assertEqual(raised.exception.stage, "token")
                self.assertEqual(tokens.get_calls, 1)
                self.assertEqual(calls, [])
                self.assertNotIn("secret-marker", str(raised.exception))
                self.assertNotIn("secret-marker", repr(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertIsNone(raised.exception.__context__)

    # Break caught: an unexpected transport exception inviting caller retry.
    def test_create_sanitizes_unexpected_transport_failure_as_unknown_once(self) -> None:
        calls = 0

        def transport(*_arguments):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            raise LookupError("unexpected-transport-secret-marker")

        creator = FamilyIssueCreator(transport=transport)

        with self.assertRaises(SendOutcomeUnknown) as raised:
            creator.create(self.binding, CANONICAL, self.tokens)  # type: ignore[arg-type]

        self.assertEqual(raised.exception.stage, "send")
        self.assertEqual(calls, 1)
        self.assertEqual(self.tokens.get_calls, 1)
        self.assertNotIn("secret-marker", str(raised.exception))
        self.assertNotIn("secret-marker", repr(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    # Break caught: forged inputs reaching token acquisition or a remote endpoint.
    def test_rejects_forged_binding_and_canonical_before_side_effects(self) -> None:
        invalid_bindings = (
            FamilyBinding(Repository("Family", "roadmap"), 42),
            FamilyBinding(Repository("family", "roadmap"), 0),
            FamilyBinding(Repository("family", "roadmap"), True),
        )
        invalid_canonicals = (
            CanonicalFamilyIssue("", CANONICAL.body),
            CanonicalFamilyIssue("x" * 257, CANONICAL.body),
            CanonicalFamilyIssue(CANONICAL.title, ""),
            CanonicalFamilyIssue(CANONICAL.title, "x" * (64 * 1024 + 1)),
            CanonicalFamilyIssue("bad\ud800", CANONICAL.body),
        )
        for binding in invalid_bindings:
            with self.subTest(binding=binding):
                creator, calls = creator_subject(issue_response())
                tokens = FakeTokens()
                with self.assertRaises(ValueError):
                    creator.create(binding, CANONICAL, tokens)  # type: ignore[arg-type]
                self.assertEqual(tokens.get_calls, 0)
                self.assertEqual(calls, [])
        for canonical in invalid_canonicals:
            with self.subTest(canonical=repr(canonical)):
                creator, calls = creator_subject(issue_response())
                tokens = FakeTokens()
                with self.assertRaises(ValueError):
                    creator.create(self.binding, canonical, tokens)  # type: ignore[arg-type]
                self.assertEqual(tokens.get_calls, 0)
                self.assertEqual(calls, [])

    # Break caught: reconciliation searching, mutating, or accepting content drift.
    def test_verifies_one_exact_existing_issue_by_positive_number(self) -> None:
        creator, calls = creator_subject(issue_response(status=200))

        created = creator.verify_existing(
            self.binding, CANONICAL, 73, self.tokens  # type: ignore[arg-type]
        )

        self.assertEqual(created, CreatedIssue(73, ISSUE_URL))
        self.assertEqual(self.tokens.get_calls, 1)
        self.assertEqual(
            calls,
            [
                (
                    "GET",
                    CREATE_URL + "/73",
                    {
                        "Accept": "application/vnd.github+json",
                        "Authorization": "Bearer family-installation-token-marker",
                        "X-GitHub-Api-Version": API_VERSION,
                        "User-Agent": "agent-container-family-approval",
                    },
                    None,
                )
            ],
        )

    # Break caught: reconciliation treating approximate metadata as proof.
    def test_reconciliation_requires_exact_number_content_repository_and_open_state(self) -> None:
        exact = complete_issue_fixture()
        response_cases = (
            issue_response(status=201),
            issue_response(payload=exact | {"number": 74}, status=200),
            issue_response(payload=exact | {"title": CANONICAL.title + " changed"}, status=200),
            issue_response(payload=exact | {"body": CANONICAL.body + "changed"}, status=200),
            issue_response(payload=exact | {"state": "closed"}, status=200),
            issue_response(
                payload=exact
                | {"html_url": "https://github.com/family/roadmap/issues/74"},
                status=200,
            ),
            issue_response(
                payload=exact
                | {"repository_url": "https://api.github.com/repos/family/other"},
                status=200,
            ),
            HttpResponse(200, {"Content-Type": "application/json"}, b"{}\n"),
        )
        for response in response_cases:
            with self.subTest(status=response.status, body=response.body[-32:]):
                creator, calls = creator_subject(response, issue_response(status=200))
                tokens = FakeTokens()

                with self.assertRaisesRegex(RuntimeError, "reconciliation failed"):
                    creator.verify_existing(
                        self.binding, CANONICAL, 73, tokens  # type: ignore[arg-type]
                    )

                self.assertEqual(len(calls), 1)
                self.assertEqual(tokens.get_calls, 1)

    # Break caught: zero/boolean/oversized numbers triggering a broader lookup.
    def test_reconciliation_rejects_invalid_number_before_side_effects(self) -> None:
        for issue_number in (True, 0, -1, MAX_ISSUE_NUMBER + 1):
            with self.subTest(issue_number=issue_number):
                creator, calls = creator_subject(issue_response(status=200))
                tokens = FakeTokens()
                with self.assertRaises(ValueError):
                    creator.verify_existing(
                        self.binding,
                        CANONICAL,
                        issue_number,  # type: ignore[arg-type]
                        tokens,  # type: ignore[arg-type]
                    )
                self.assertEqual(tokens.get_calls, 0)
                self.assertEqual(calls, [])


class FailureContractTest(unittest.TestCase):
    # Break caught: arbitrary stage/error text leaking into public exceptions or audit.
    def test_failure_types_accept_only_their_fixed_secret_free_stages(self) -> None:
        for exception_type, stages, message in (
            (SendNotStarted, ("token", "send"), "family Issue send did not start"),
            (SendOutcomeUnknown, ("send", "response"), "family Issue send outcome is unknown"),
        ):
            for stage in stages:
                with self.subTest(exception=exception_type.__name__, stage=stage):
                    error = exception_type(stage)
                    self.assertEqual(error.stage, stage)
                    self.assertEqual(str(error), message)
                    self.assertEqual(
                        repr(error), f"{exception_type.__name__}({message!r})"
                    )
            for stage in ("", "secret-marker", "reconcile", None, True):
                with self.subTest(exception=exception_type.__name__, rejected=stage):
                    with self.assertRaises(ValueError) as raised:
                        exception_type(stage)  # type: ignore[arg-type]
                    self.assertNotIn("secret-marker", str(raised.exception))


class FakeHTTPResponse:
    status = 201

    def __init__(
        self,
        body: bytes | None = None,
        *,
        headers: list[tuple[str, str]] | None = None,
        length: int | None = None,
        chunked: bool = False,
        complete: bool = True,
    ) -> None:
        self.body = issue_response().body if body is None else body
        self.headers = (
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(self.body))),
            ]
            if headers is None
            else headers
        )
        self.length = len(self.body) if length is None and not chunked else length
        self.chunked = chunked
        self.complete = complete
        self.closed_by_read = False
        self.closed = 0
        self.offset = 0
        self.read_maximum: int | None = None

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self.headers)

    def read(self, maximum: int) -> bytes:
        self.read_maximum = maximum
        chunk = self.body[self.offset : self.offset + maximum]
        self.offset += len(chunk)
        if self.length is not None:
            self.length -= len(chunk)
            if self.length == 0 and self.complete:
                self.closed_by_read = True
        elif self.complete:
            self.closed_by_read = True
        return chunk

    def isclosed(self) -> bool:
        return self.closed_by_read

    def close(self) -> None:
        self.closed += 1


class InstrumentedHTTPSConnection:
    response = FakeHTTPResponse()
    failure_stage: str | None = None
    failure: Exception = ConnectionResetError("socket-secret-marker")
    close_failure: Exception | None = None
    instances: list["InstrumentedHTTPSConnection"] = []

    def __init__(self, host: str, *, timeout: int) -> None:
        self.host = host
        self.timeout = timeout
        self.events: list[object] = []
        self.closed = 0
        type(self).instances.append(self)

    def _fail(self, stage: str) -> None:
        if self.failure_stage == stage:
            raise self.failure

    def connect(self) -> None:
        self.events.append("connect")
        self._fail("connect")

    def putrequest(
        self,
        method: str,
        path: str,
        *,
        skip_host: bool,
        skip_accept_encoding: bool,
    ) -> None:
        self.events.append(
            ("putrequest", method, path, skip_host, skip_accept_encoding)
        )
        self._fail("putrequest")

    def putheader(self, name: str, value: str) -> None:
        self.events.append(("putheader", name, value))
        self._fail(f"putheader:{name}")

    def endheaders(self) -> None:
        self.events.append("endheaders")
        self._fail("endheaders")

    def send(self, body: bytes) -> None:
        self.events.append(("send", body))
        self._fail("send")

    def getresponse(self) -> FakeHTTPResponse:
        self.events.append("getresponse")
        self._fail("getresponse")
        return self.response

    def close(self) -> None:
        self.closed += 1
        if self.close_failure is not None:
            raise self.close_failure


class FamilyIssueTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        InstrumentedHTTPSConnection.instances = []
        InstrumentedHTTPSConnection.failure_stage = None
        InstrumentedHTTPSConnection.failure = ConnectionResetError(
            "socket-secret-marker"
        )
        InstrumentedHTTPSConnection.close_failure = None
        InstrumentedHTTPSConnection.response = FakeHTTPResponse()
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer family-installation-token-marker",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "agent-container-family-approval",
        }

    def call_transport(self) -> HttpResponse:
        with mock.patch(
            "agent_container.family_issue_create.http.client.HTTPSConnection",
            InstrumentedHTTPSConnection,
        ):
            return family_issue_transport(
                "POST", CREATE_URL, self.headers, EXPECTED_BODY
            )

    # Break caught: buffering the whole request through request() hides body-send state.
    def test_real_transport_uses_one_explicit_body_send_at_the_fixed_origin(self) -> None:
        response = self.call_transport()

        self.assertEqual(response, framed_issue_response())
        self.assertEqual(len(InstrumentedHTTPSConnection.instances), 1)
        connection = InstrumentedHTTPSConnection.instances[0]
        self.assertEqual(connection.host, "api.github.com")
        self.assertEqual(connection.timeout, 30)
        self.assertEqual(
            connection.events,
            [
                "connect",
                ("putrequest", "POST", "/repos/family/roadmap/issues", True, True),
                ("putheader", "Host", "api.github.com"),
                ("putheader", "Accept", "application/vnd.github+json"),
                (
                    "putheader",
                    "Authorization",
                    "Bearer family-installation-token-marker",
                ),
                ("putheader", "Content-Type", "application/json"),
                ("putheader", "X-GitHub-Api-Version", API_VERSION),
                (
                    "putheader",
                    "User-Agent",
                    "agent-container-family-approval",
                ),
                ("putheader", "Content-Length", str(len(EXPECTED_BODY))),
                "endheaders",
                ("send", EXPECTED_BODY),
                "getresponse",
            ],
        )
        self.assertEqual(
            InstrumentedHTTPSConnection.response.read_maximum,
            MAX_ISSUE_RESPONSE_BYTES + 1,
        )
        self.assertEqual(InstrumentedHTTPSConnection.response.closed, 1)
        self.assertEqual(connection.closed, 1)

    # Break caught: classifying DNS/connect/TLS/header failures as possibly created.
    def test_failures_before_body_send_begins_are_proven_not_started(self) -> None:
        for stage in ("connect", "putrequest", "putheader:Host", "endheaders"):
            with self.subTest(stage=stage):
                InstrumentedHTTPSConnection.instances = []
                InstrumentedHTTPSConnection.failure_stage = stage

                with self.assertRaises(SendNotStarted) as raised:
                    self.call_transport()

                self.assertEqual(raised.exception.stage, "send")
                connection = InstrumentedHTTPSConnection.instances[0]
                self.assertFalse(
                    any(
                        isinstance(event, tuple) and event[0] == "send"
                        for event in connection.events
                    )
                )
                self.assertEqual(connection.closed, 1)
                self.assertNotIn("secret", str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)

    # Break caught: treating a send() reset as safe to retry despite partial writes.
    def test_failure_once_body_send_is_entered_has_unknown_outcome(self) -> None:
        InstrumentedHTTPSConnection.failure_stage = "send"

        with self.assertRaises(SendOutcomeUnknown) as raised:
            self.call_transport()

        self.assertEqual(raised.exception.stage, "send")
        connection = InstrumentedHTTPSConnection.instances[0]
        self.assertEqual(
            [event for event in connection.events if isinstance(event, tuple) and event[0] == "send"],
            [("send", EXPECTED_BODY)],
        )
        self.assertEqual(connection.closed, 1)
        self.assertIsNone(raised.exception.__cause__)

    # Break caught: timeout/reset/malformed status after sending being retried as safe.
    def test_failure_while_waiting_for_response_has_unknown_outcome(self) -> None:
        InstrumentedHTTPSConnection.failure_stage = "getresponse"

        with self.assertRaises(SendOutcomeUnknown) as raised:
            self.call_transport()

        self.assertEqual(raised.exception.stage, "response")
        connection = InstrumentedHTTPSConnection.instances[0]
        self.assertEqual(
            sum(
                isinstance(event, tuple) and event[0] == "send"
                for event in connection.events
            ),
            1,
        )
        self.assertEqual(connection.closed, 1)
        self.assertIsNone(raised.exception.__cause__)

    # Break caught: ordinary exceptions bypassing the phase-only safety funnel.
    def test_every_ordinary_http_phase_exception_is_fixed_and_one_attempt(self) -> None:
        cases = (
            ("connect", TypeError("connect-secret-marker"), SendNotStarted, "send"),
            ("send", AttributeError("send-secret-marker"), SendOutcomeUnknown, "send"),
            (
                "getresponse",
                TypeError("response-secret-marker"),
                SendOutcomeUnknown,
                "response",
            ),
        )
        for stage, failure, error_type, expected_stage in cases:
            with self.subTest(stage=stage, failure=type(failure).__name__):
                InstrumentedHTTPSConnection.instances = []
                InstrumentedHTTPSConnection.failure_stage = stage
                InstrumentedHTTPSConnection.failure = failure

                with self.assertRaises(error_type) as raised:
                    self.call_transport()

                self.assertEqual(raised.exception.stage, expected_stage)
                self.assertEqual(len(InstrumentedHTTPSConnection.instances), 1)
                self.assertNotIn("secret-marker", str(raised.exception))
                self.assertNotIn("secret-marker", repr(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertIsNone(raised.exception.__context__)

    # Break caught: read and cleanup exceptions escaping or overriding outcome proof.
    def test_read_and_cleanup_exceptions_are_sanitized_without_retry(self) -> None:
        class ExplodingResponse(FakeHTTPResponse):
            def read(self, maximum: int) -> bytes:
                self.read_maximum = maximum
                raise TypeError("read-secret-marker")

            def close(self) -> None:
                self.closed += 1
                raise AttributeError("response-close-secret-marker")

        InstrumentedHTTPSConnection.response = ExplodingResponse()
        InstrumentedHTTPSConnection.close_failure = TypeError(
            "connection-close-secret-marker"
        )

        with self.assertRaises(SendOutcomeUnknown) as raised:
            self.call_transport()

        self.assertEqual(raised.exception.stage, "response")
        self.assertEqual(len(InstrumentedHTTPSConnection.instances), 1)
        self.assertEqual(InstrumentedHTTPSConnection.response.closed, 1)
        self.assertEqual(InstrumentedHTTPSConnection.instances[0].closed, 1)
        self.assertNotIn("secret-marker", str(raised.exception))
        self.assertNotIn("secret-marker", repr(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    # Break caught: a close-only failure overriding a fully captured response.
    def test_cleanup_type_and_attribute_errors_do_not_override_success(self) -> None:
        class CloseFailingResponse(FakeHTTPResponse):
            def close(self) -> None:
                self.closed += 1
                raise TypeError("response-close-secret-marker")

        InstrumentedHTTPSConnection.response = CloseFailingResponse()
        InstrumentedHTTPSConnection.close_failure = AttributeError(
            "connection-close-secret-marker"
        )

        response = self.call_transport()

        self.assertEqual(response, framed_issue_response())
        self.assertEqual(len(InstrumentedHTTPSConnection.instances), 1)
        self.assertEqual(InstrumentedHTTPSConnection.response.closed, 1)
        self.assertEqual(InstrumentedHTTPSConnection.instances[0].closed, 1)

    # Break caught: trusting a domain error whose stage contradicts send progress.
    def test_domain_errors_are_preserved_only_when_phase_consistent(self) -> None:
        cases = (
            ("connect", SendOutcomeUnknown("response"), SendNotStarted, "send"),
            ("send", SendNotStarted("send"), SendOutcomeUnknown, "send"),
            (
                "getresponse",
                SendOutcomeUnknown("send"),
                SendOutcomeUnknown,
                "response",
            ),
        )
        for stage, failure, error_type, expected_stage in cases:
            with self.subTest(stage=stage, failure=type(failure).__name__):
                InstrumentedHTTPSConnection.instances = []
                InstrumentedHTTPSConnection.failure_stage = stage
                InstrumentedHTTPSConnection.failure = failure

                with self.assertRaises(error_type) as raised:
                    self.call_transport()

                self.assertEqual(raised.exception.stage, expected_stage)
                self.assertEqual(len(InstrumentedHTTPSConnection.instances), 1)
                self.assertIsNone(raised.exception.__cause__)

    # Break caught: creator downgrading a proven default-transport pre-send failure.
    def test_creator_preserves_default_transport_pre_body_classification(self) -> None:
        InstrumentedHTTPSConnection.failure_stage = "connect"
        InstrumentedHTTPSConnection.failure = TypeError(
            "connect-secret-marker"
        )
        creator = FamilyIssueCreator()
        tokens = FakeTokens()
        binding = FamilyBinding(Repository("family", "roadmap"), 42)

        with mock.patch(
            "agent_container.family_issue_create.http.client.HTTPSConnection",
            InstrumentedHTTPSConnection,
        ):
            with self.assertRaises(SendNotStarted) as raised:
                creator.create(binding, CANONICAL, tokens)  # type: ignore[arg-type]

        self.assertEqual(raised.exception.stage, "send")
        self.assertEqual(tokens.get_calls, 1)
        self.assertEqual(len(InstrumentedHTTPSConnection.instances), 1)
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
        self.assertNotIn("secret-marker", str(raised.exception))
        self.assertNotIn("secret-marker", repr(raised.exception))

    # Break caught: a partial response body escaping as a generic, retryable error.
    def test_partial_response_failure_after_send_has_unknown_outcome(self) -> None:
        class PartialResponse(FakeHTTPResponse):
            def read(self, maximum: int) -> bytes:
                self.read_maximum = maximum
                raise http.client.IncompleteRead(b'{"number":73')

        InstrumentedHTTPSConnection.response = PartialResponse()

        with self.assertRaises(SendOutcomeUnknown) as raised:
            self.call_transport()

        self.assertEqual(raised.exception.stage, "response")
        self.assertEqual(InstrumentedHTTPSConnection.response.closed, 1)
        self.assertEqual(InstrumentedHTTPSConnection.instances[0].closed, 1)

    # Break caught: accepting a different host/path/method or extra request fields.
    def test_transport_rejects_every_non_fixed_endpoint_and_request_shape(self) -> None:
        invalid = (
            ("PUT", CREATE_URL, self.headers, EXPECTED_BODY),
            ("GET", CREATE_URL, self.headers, None),
            ("POST", "https://evil.invalid/repos/family/roadmap/issues", self.headers, EXPECTED_BODY),
            ("POST", CREATE_URL + "/73", self.headers, EXPECTED_BODY),
            ("POST", CREATE_URL + "?labels=bug", self.headers, EXPECTED_BODY),
            ("POST", "http://api.github.com/repos/family/roadmap/issues", self.headers, EXPECTED_BODY),
            ("POST", CREATE_URL, self.headers, EXPECTED_BODY[:-1] + b',"labels":[] }'),
            ("POST", CREATE_URL, self.headers | {"X-Extra": "value"}, EXPECTED_BODY),
        )
        for method, url, headers, body in invalid:
            with self.subTest(method=method, url=url, body=body):
                with self.assertRaises(ValueError):
                    family_issue_transport(method, url, headers, body)
        self.assertEqual(InstrumentedHTTPSConnection.instances, [])

    # Break caught: response-size enforcement relying only on the high-level parser.
    def test_transport_rejects_oversized_response_after_one_send(self) -> None:
        InstrumentedHTTPSConnection.response = FakeHTTPResponse(
            b"x" * (MAX_ISSUE_RESPONSE_BYTES + 1)
        )

        with self.assertRaises(SendOutcomeUnknown) as raised:
            self.call_transport()

        self.assertEqual(raised.exception.stage, "response")
        self.assertEqual(len(InstrumentedHTTPSConnection.instances), 1)
        self.assertEqual(
            sum(
                isinstance(event, tuple) and event[0] == "send"
                for event in InstrumentedHTTPSConnection.instances[0].events
            ),
            1,
        )

    # Break caught: bounded read() returning valid JSON before declared EOF.
    def test_real_http_response_short_read_with_remaining_length_is_unknown(self) -> None:
        body = issue_response().body
        raw = (
            b"HTTP/1.1 201 Created\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body) + 17}\r\n".encode("ascii")
            + b"\r\n"
            + body
        )

        class MemorySocket:
            def makefile(self, mode: str):
                self.mode = mode
                return BytesIO(raw)

        response = http.client.HTTPResponse(MemorySocket())  # type: ignore[arg-type]
        response.begin()
        self.assertEqual(response.length, len(body) + 17)
        InstrumentedHTTPSConnection.response = response  # type: ignore[assignment]

        with self.assertRaises(SendOutcomeUnknown) as raised:
            self.call_transport()

        self.assertEqual(raised.exception.stage, "response")
        self.assertEqual(len(InstrumentedHTTPSConnection.instances), 1)
        self.assertNotIn(body.decode("ascii")[:20], str(raised.exception))

    # Break caught: completeness checks rejecting a fully terminated chunked body.
    def test_real_complete_chunked_response_remains_accepted(self) -> None:
        body = issue_response().body
        raw = (
            b"HTTP/1.1 201 Created\r\n"
            b"Content-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            + f"{len(body):x}\r\n".encode("ascii")
            + body
            + b"\r\n0\r\n\r\n"
        )

        class MemorySocket:
            def makefile(self, mode: str):
                self.mode = mode
                return BytesIO(raw)

        response = http.client.HTTPResponse(MemorySocket())  # type: ignore[arg-type]
        response.begin()
        self.assertTrue(response.chunked)
        InstrumentedHTTPSConnection.response = response  # type: ignore[assignment]

        captured = self.call_transport()

        self.assertEqual(
            captured,
            HttpResponse(
                201,
                {
                    "Content-Type": "application/json",
                    "Transfer-Encoding": "chunked",
                },
                body,
            ),
        )

    # Break caught: raw mixed-case duplicate framing headers collapsing in dict().
    def test_raw_duplicate_content_type_and_length_headers_are_unknown(self) -> None:
        body = issue_response().body
        cases = (
            [
                ("Content-Type", "application/json"),
                ("content-type", "application/json"),
                ("Content-Length", str(len(body))),
            ],
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
                ("content-length", str(len(body))),
            ],
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
                ("content-length", str(len(body) + 1)),
            ],
        )
        for headers in cases:
            with self.subTest(headers=headers):
                InstrumentedHTTPSConnection.instances = []
                InstrumentedHTTPSConnection.response = FakeHTTPResponse(
                    headers=headers,
                    length=len(body),
                )

                with self.assertRaises(SendOutcomeUnknown) as raised:
                    self.call_transport()

                self.assertEqual(raised.exception.stage, "response")
                self.assertEqual(len(InstrumentedHTTPSConnection.instances), 1)

    # Break caught: incomplete chunk framing being mistaken for a complete JSON body.
    def test_unfinished_chunked_response_state_is_unknown(self) -> None:
        InstrumentedHTTPSConnection.response = FakeHTTPResponse(
            headers=[("Content-Type", "application/json")],
            chunked=True,
            complete=False,
        )

        with self.assertRaises(SendOutcomeUnknown) as raised:
            self.call_transport()

        self.assertEqual(raised.exception.stage, "response")
        self.assertEqual(len(InstrumentedHTTPSConnection.instances), 1)

    # Break caught: reconciliation transport using a collection/search/mutation route.
    def test_transport_permits_only_exact_positive_issue_get_without_body(self) -> None:
        get_headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer family-installation-token-marker",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "agent-container-family-approval",
        }
        InstrumentedHTTPSConnection.response.status = 200
        with mock.patch(
            "agent_container.family_issue_create.http.client.HTTPSConnection",
            InstrumentedHTTPSConnection,
        ):
            response = family_issue_transport(
                "GET", CREATE_URL + "/73", get_headers, None
            )
        self.assertEqual(response.status, 200)
        connection = InstrumentedHTTPSConnection.instances[0]
        self.assertEqual(
            connection.events[1],
            ("putrequest", "GET", "/repos/family/roadmap/issues/73", True, True),
        )
        self.assertFalse(
            any(
                isinstance(event, tuple) and event[0] == "send"
                for event in connection.events
            )
        )
        for method, url, body in (
            ("GET", CREATE_URL, None),
            ("GET", CREATE_URL + "/0", None),
            ("GET", CREATE_URL + "/073", None),
            ("GET", CREATE_URL + "/73?state=open", None),
            ("GET", CREATE_URL + "/73", b""),
            ("PATCH", CREATE_URL + "/73", None),
        ):
            with self.subTest(method=method, url=url, body=body):
                with self.assertRaises(ValueError):
                    family_issue_transport(method, url, get_headers, body)


if __name__ == "__main__":
    unittest.main()
