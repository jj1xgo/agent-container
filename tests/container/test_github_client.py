from contextlib import redirect_stderr
from io import BytesIO, StringIO
import json
import struct
import unittest
from unittest import mock

import agent_container.github_client as github_client
from agent_container.github_broker_protocol import BrokerResponse
from agent_container.github_broker_protocol import decode_request_frame
from agent_container.github_broker_protocol import encode_response_frame
from agent_container.github_broker_protocol import write_chunk_stream
from agent_container.github_client import run
from agent_container.github_issue import MAX_ISSUE_RESPONSE_BYTES


class Duplex:
    def __init__(self, incoming: bytes) -> None:
        self.incoming = BytesIO(incoming)
        self.outgoing = BytesIO()
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self.incoming.read(size)

    def write(self, body: bytes) -> int:
        return self.outgoing.write(body)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeSocket:
    def __init__(self, stream: Duplex) -> None:
        self.stream = stream
        self.connected = None
        self.closed = False

    def settimeout(self, timeout: int) -> None:
        self.timeout = timeout

    def connect(self, path: str) -> None:
        self.connected = path

    def makefile(self, *_: object, **__: object) -> Duplex:
        return self.stream

    def close(self) -> None:
        self.closed = True


def response(result: object) -> bytes:
    stream = BytesIO()
    stream.write(encode_response_frame(BrokerResponse(1, "ok")))
    write_chunk_stream(stream, (json.dumps(result).encode(),))
    return stream.getvalue()


def issue_summary(number: int = 12) -> dict[str, object]:
    return {
        "number": number,
        "title": "Issue title",
        "state": "open",
        "author": "octocat",
        "labels": ["bug"],
        "created_at": "2026-08-28T00:00:00Z",
        "updated_at": "2026-08-28T01:00:00Z",
        "url": f"https://github.com/jj1xgo/agent-container/issues/{number}",
    }


def pull_request_summary(number: int = 12) -> dict[str, object]:
    return {
        "number": number,
        "state": "open",
        "title": "Feature",
        "url": f"https://github.com/jj1xgo/agent-container/pull/{number}",
    }


class GitHubClientTest(unittest.TestCase):
    def test_cli_exposes_only_fixed_pull_request_operations(self) -> None:
        calls = []

        def requester(operation, payload, environment):  # type: ignore[no-untyped-def]
            calls.append((operation, payload, environment))
            return {"number": 12, "state": "open"}

        stdout, stderr = StringIO(), StringIO()
        status = run(
            [
                "pr", "create", "--base", "main", "--head", "feat/work",
                "--title", "Feature", "--body", "Body",
            ],
            {"AGENT_PROJECT_ID": "agent-container"},
            stdout,
            stderr,
            requester=requester,
        )
        self.assertEqual(status, 0)
        self.assertEqual(calls[0][0], "pr-create")
        self.assertEqual(calls[0][1]["head"], "feat/work")
        self.assertEqual(json.loads(stdout.getvalue())["number"], 12)
        self.assertEqual(stderr.getvalue(), "")

        for arguments, operation in (
            (["pr", "view", "12"], "pr-view"),
            (["pr", "checks", "12"], "pr-checks"),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    run(
                        arguments,
                        {},
                        StringIO(),
                        StringIO(),
                        requester=requester,
                    ),
                    0,
                )
                self.assertEqual(calls[-1][0], operation)
                self.assertEqual(calls[-1][1], {"number": 12})

    def test_cli_exposes_only_fixed_issue_read_operations(self) -> None:
        calls = []

        def requester(operation, payload, environment):  # type: ignore[no-untyped-def]
            calls.append((operation, payload, environment))
            if operation == "issue-list":
                return {"issues": []}
            return issue_summary() | {"body": "Body"}

        environment = {"AGENT_PROJECT_ID": "agent-container"}
        for arguments, operation, payload, expected in (
            (["issue", "list"], "issue-list", {}, {"issues": []}),
            (
                ["issue", "view", "12"],
                "issue-view",
                {"number": 12},
                issue_summary() | {"body": "Body"},
            ),
        ):
            with self.subTest(arguments=arguments):
                stdout, stderr = StringIO(), StringIO()
                self.assertEqual(
                    run(arguments, environment, stdout, stderr, requester=requester),
                    0,
                )
                self.assertEqual(calls[-1][:2], (operation, payload))
                self.assertEqual(json.loads(stdout.getvalue()), expected)
                self.assertEqual(stderr.getvalue(), "")

    def test_cli_rejects_adjacent_issue_operations_and_options_before_request(
        self,
    ) -> None:
        calls = []

        def requester(*arguments):  # type: ignore[no-untyped-def]
            calls.append(arguments)
            return {}

        rejected = (
            ["issue", "create"],
            ["issue", "comment", "12"],
            ["issue", "list", "--page", "2"],
            ["issue", "list", "--repository", "other/repo"],
            ["issue", "view", "12", "--repository", "other/repo"],
            ["issue", "view", "0"],
            ["issue", "view", "-1"],
            ["issue", "view", "not-a-number"],
        )
        for arguments in rejected:
            with self.subTest(arguments=arguments), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    run(arguments, {}, StringIO(), StringIO(), requester=requester)
        self.assertEqual(calls, [])

    @mock.patch("agent_container.github_client.read_broker_capability", return_value="c" * 43)
    @mock.patch("agent_container.github_client.validate_broker_socket")
    def test_request_crosses_socket_without_repository_or_token(
        self, validate_socket: mock.Mock, read_capability: mock.Mock
    ) -> None:
        expected = issue_summary() | {"body": "Body"}
        stream = Duplex(response(expected))
        client = FakeSocket(stream)
        result = github_client.request_github_operation(
            "issue-view",
            {"number": 12},
            {
                "AGENT_BROKER_SOCKET": "/run/broker.sock",
                "AGENT_BROKER_CAPABILITY": "/run/capability",
                "AGENT_PROJECT_ID": "agent-container",
            },
            socket_factory=lambda *_: client,
        )
        request, _ = decode_request_frame(stream.outgoing.getvalue())
        self.assertEqual(result, expected)
        self.assertEqual(request.operation, "issue-view")
        self.assertEqual(request.payload, {"number": 12})
        self.assertNotIn(b"repository", stream.outgoing.getvalue())
        self.assertNotIn(b"token", stream.outgoing.getvalue().lower())
        self.assertEqual(client.connected, "/run/broker.sock")
        self.assertTrue(stream.closed)
        self.assertTrue(client.closed)

    @mock.patch("agent_container.github_client.read_broker_capability", return_value="secret-marker")
    @mock.patch("agent_container.github_client.validate_broker_socket")
    def test_denial_is_secret_free(
        self, validate_socket: mock.Mock, read_capability: mock.Mock
    ) -> None:
        client = FakeSocket(Duplex(encode_response_frame(BrokerResponse(1, "denied"))))
        with self.assertRaises(RuntimeError) as raised:
            github_client.request_github_operation(
                "pr-view", {"number": 12},
                {"AGENT_BROKER_SOCKET": "/run/broker.sock", "AGENT_BROKER_CAPABILITY": "/run/capability"},
                socket_factory=lambda *_: client,
            )
        self.assertNotIn("secret-marker", str(raised.exception))
        self.assertTrue(client.stream.closed)
        self.assertTrue(client.closed)

    @mock.patch("agent_container.github_client.read_broker_capability", return_value="c" * 43)
    @mock.patch("agent_container.github_client.validate_broker_socket")
    def test_request_rejects_invalid_json_streams_and_closes_resources(
        self, validate_socket: mock.Mock, read_capability: mock.Mock
    ) -> None:
        ok = encode_response_frame(BrokerResponse(1, "ok"))
        oversized = BytesIO(ok)
        write_chunk_stream(
            oversized,
            (
                b"x" * (MAX_ISSUE_RESPONSE_BYTES // 2),
                b"x" * (MAX_ISSUE_RESPONSE_BYTES // 2),
                b"x",
            ),
        )
        malformed = BytesIO(ok)
        write_chunk_stream(malformed, (b"{",))
        array = BytesIO(ok)
        write_chunk_stream(array, (b"[]",))
        truncated = ok + struct.pack(">I", 5) + b"{}"

        for incoming in (
            oversized.getvalue(),
            malformed.getvalue(),
            array.getvalue(),
            truncated,
        ):
            with self.subTest(size=len(incoming)):
                stream = Duplex(incoming)
                client = FakeSocket(stream)
                with self.assertRaises(ValueError):
                    github_client.request_github_operation(
                        "issue-list",
                        {},
                        {
                            "AGENT_BROKER_SOCKET": "/run/broker.sock",
                            "AGENT_BROKER_CAPABILITY": "/run/capability",
                        },
                        socket_factory=lambda *_: client,
                    )
                self.assertTrue(stream.closed)
                self.assertTrue(client.closed)

    @mock.patch("agent_container.github_client.read_broker_capability", return_value="c" * 43)
    @mock.patch("agent_container.github_client.validate_broker_socket")
    def test_request_rejects_operation_specific_schema_mismatch(
        self, validate_socket: mock.Mock, read_capability: mock.Mock
    ) -> None:
        invalid = (
            ("list-not-list", "issue-list", {"issues": "not-a-list"}),
            (
                "list-extra-key",
                "issue-list",
                {"issues": [issue_summary()], "extra": True},
            ),
            ("list-too-many", "issue-list", {"issues": [issue_summary()] * 31}),
            (
                "list-closed",
                "issue-list",
                {"issues": [issue_summary() | {"state": "closed"}]},
            ),
            ("view-missing-body", "issue-view", issue_summary()),
            ("view-body-type", "issue-view", issue_summary() | {"body": 1}),
            (
                "view-extra-key",
                "issue-view",
                issue_summary() | {"body": "Body", "extra": True},
            ),
            (
                "view-boolean-number",
                "issue-view",
                issue_summary() | {"number": True, "body": "Body"},
            ),
            (
                "view-title-bound",
                "issue-view",
                issue_summary() | {"title": "x" * 257, "body": "Body"},
            ),
            (
                "view-state-value",
                "issue-view",
                issue_summary() | {"state": "pending", "body": "Body"},
            ),
            (
                "view-state-type",
                "issue-view",
                issue_summary() | {"state": [], "body": "Body"},
            ),
            (
                "view-author-type",
                "issue-view",
                issue_summary() | {"author": 1, "body": "Body"},
            ),
            (
                "view-label-bound",
                "issue-view",
                issue_summary() | {"labels": ["x" * 101], "body": "Body"},
            ),
            (
                "view-label-count",
                "issue-view",
                issue_summary() | {"labels": ["x"] * 101, "body": "Body"},
            ),
            (
                "view-timestamp",
                "issue-view",
                issue_summary() | {"created_at": "today", "body": "Body"},
            ),
            (
                "view-url-number",
                "issue-view",
                issue_summary()
                | {
                    "url": "https://github.com/other/repo/issues/13",
                    "body": "Body",
                },
            ),
            (
                "view-request-number",
                "issue-view",
                issue_summary(13) | {"body": "Body"},
            ),
            (
                "view-body-bound",
                "issue-view",
                issue_summary() | {"body": "x" * (256 * 1024 + 1)},
            ),
        )
        for name, operation, result in invalid:
            with self.subTest(name=name):
                stream = Duplex(response(result))
                client = FakeSocket(stream)
                with self.assertRaises(ValueError):
                    github_client.request_github_operation(
                        operation,
                        {} if operation == "issue-list" else {"number": 12},
                        {
                            "AGENT_BROKER_SOCKET": "/run/broker.sock",
                            "AGENT_BROKER_CAPABILITY": "/run/capability",
                        },
                        socket_factory=lambda *_: client,
                    )
                self.assertTrue(stream.closed)
                self.assertTrue(client.closed)

    @mock.patch("agent_container.github_client.read_broker_capability", return_value="c" * 43)
    @mock.patch("agent_container.github_client.validate_broker_socket")
    def test_request_accepts_exact_pull_request_schemas(
        self, validate_socket: mock.Mock, read_capability: mock.Mock
    ) -> None:
        accepted = (
            (
                "pr-create",
                {"base": "main", "head": "feat/work", "title": "Feature", "body": ""},
                pull_request_summary(),
            ),
            ("pr-view", {"number": 12}, pull_request_summary()),
            (
                "pr-checks",
                {"number": 12},
                {
                    "number": 12,
                    "checks": [
                        {
                            "name": "tests",
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ],
                },
            ),
        )
        for operation, payload, expected in accepted:
            with self.subTest(operation=operation):
                stream = Duplex(response(expected))
                client = FakeSocket(stream)
                self.assertEqual(
                    github_client.request_github_operation(
                        operation,
                        payload,
                        {
                            "AGENT_BROKER_SOCKET": "/run/broker.sock",
                            "AGENT_BROKER_CAPABILITY": "/run/capability",
                        },
                        socket_factory=lambda *_: client,
                    ),
                    expected,
                )
                self.assertTrue(stream.closed)
                self.assertTrue(client.closed)

    @mock.patch("agent_container.github_client.read_broker_capability", return_value="c" * 43)
    @mock.patch("agent_container.github_client.validate_broker_socket")
    def test_request_rejects_pull_request_schema_mismatch(
        self, validate_socket: mock.Mock, read_capability: mock.Mock
    ) -> None:
        invalid = (
            ("pr-view", {"number": 12, "state": "open"}),
            ("pr-view", pull_request_summary(13)),
            (
                "pr-checks",
                {
                    "number": 12,
                    "checks": [
                        {
                            "name": "tests",
                            "status": "completed",
                            "conclusion": "success",
                            "extra": True,
                        }
                    ],
                },
            ),
        )
        for operation, result in invalid:
            with self.subTest(operation=operation):
                stream = Duplex(response(result))
                client = FakeSocket(stream)
                with self.assertRaises(ValueError):
                    github_client.request_github_operation(
                        operation,
                        {"number": 12},
                        {
                            "AGENT_BROKER_SOCKET": "/run/broker.sock",
                            "AGENT_BROKER_CAPABILITY": "/run/capability",
                        },
                        socket_factory=lambda *_: client,
                    )

    @mock.patch("agent_container.github_client.read_broker_capability", return_value="c" * 43)
    @mock.patch("agent_container.github_client.validate_broker_socket")
    def test_request_accepts_exact_bounded_issue_schemas(
        self, validate_socket: mock.Mock, read_capability: mock.Mock
    ) -> None:
        accepted = (
            ("issue-list", {"issues": [issue_summary()]}),
            ("issue-view", issue_summary() | {"body": "Body\n"}),
        )
        for operation, expected in accepted:
            with self.subTest(operation=operation):
                stream = Duplex(response(expected))
                client = FakeSocket(stream)
                self.assertEqual(
                    github_client.request_github_operation(
                        operation,
                        {} if operation == "issue-list" else {"number": 12},
                        {
                            "AGENT_BROKER_SOCKET": "/run/broker.sock",
                            "AGENT_BROKER_CAPABILITY": "/run/capability",
                        },
                        socket_factory=lambda *_: client,
                    ),
                    expected,
                )
                self.assertTrue(stream.closed)
                self.assertTrue(client.closed)
