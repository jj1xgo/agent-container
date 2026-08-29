from io import BytesIO
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_container.github_app import GitHubAppMetadata
from agent_container.github_app import InstallationTokenProvider
from agent_container.github_app import github_transport
from agent_container.git_remote_helper import run_remote_helper
from agent_container.github_broker import BrokerSession
from agent_container.github_broker_error import BrokerStageError
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_broker_runtime import UploadPackBrokerRuntime
from agent_container.github_broker_transport import BrokerUploadPackClient
from agent_container.github_broker_transport import BrokerReceivePackClient
from agent_container.github_client import request_github_operation
from agent_container.github_issue import GitHubIssueTransport


RUN_SOCKET_INTEGRATION = (
    os.environ.get("AGENT_CONTAINER_RUN_SOCKET_INTEGRATION") == "1"
)


class FakeUploadPackTransport:
    def __init__(self) -> None:
        self.requests: list[bytes] = []

    def discover(self) -> bytes:
        return b"000eversion 2\n0000"

    def rpc(self, request: bytes):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return (b"0008NAK\n", b"0002")


class FailFirstUploadPackTransport(FakeUploadPackTransport):
    def __init__(self) -> None:
        super().__init__()
        self.discovery_calls = 0

    def discover(self) -> bytes:
        self.discovery_calls += 1
        if self.discovery_calls == 1:
            raise BrokerStageError("upload-discovery")
        return super().discover()


def pkt(payload: bytes) -> bytes:
    return f"{len(payload) + 4:04x}".encode("ascii") + payload


OLD = "1" * 40
NEW = "2" * 40


class FakeReceivePackTransport:
    def __init__(self) -> None:
        self.requests: list[bytes] = []
        self.advertisement = (
            pkt(
                f"{OLD} refs/heads/feat/work".encode()
                + b"\0report-status side-band-64k object-format=sha1\n"
            )
            + b"0000"
        )

    def discover(self) -> bytes:
        return self.advertisement

    def rpc(self, request: bytes):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return (b"000eunpack ok\n", b"0000")


class FakePullRequestTransport:
    def view(self, number):  # type: ignore[no-untyped-def]
        return {
            "number": number,
            "state": "open",
            "title": "Feature",
            "url": "https://github.com/jj1xgo/agent-container/pull/12",
        }


def issue_summary(number: int = 12) -> dict[str, object]:
    return {
        "number": number,
        "title": "content-title-marker",
        "state": "open",
        "author": "content-author-marker",
        "labels": ["content-label-marker"],
        "created_at": "2026-08-28T00:00:00Z",
        "updated_at": "2026-08-28T01:00:00Z",
        "url": f"https://github.com/jj1xgo/agent-container/issues/{number}",
    }


class FakeIssueTransport:
    def list_open(self) -> dict[str, object]:
        return {"issues": [issue_summary()]}

    def view(self, number: int) -> dict[str, object]:
        return issue_summary(number) | {"body": "content-body-marker"}


class FakeSigner:
    def sign(self, content: bytes, private_key: Path) -> bytes:
        return b"binary-signature"


@unittest.skipUnless(
    RUN_SOCKET_INTEGRATION,
    "set AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 for Unix socket integration",
)
class GitHubBrokerSocketIntegrationTest(unittest.TestCase):
    def test_token_protocol_failure_does_not_stop_issue_runtime(self) -> None:
        marker = "secret-malformed-token-status"

        class TokenResponse:
            status = 201
            headers = {"Content-Type": "application/json"}

            def read(self, maximum: int) -> bytes:
                return json.dumps(
                    {
                        "token": "installation-token-marker",
                        "expires_at": "2026-08-28T14:00:00Z",
                        "permissions": {
                            "checks": "read",
                            "contents": "write",
                            "issues": "read",
                            "metadata": "read",
                            "pull_requests": "write",
                        },
                        "repositories": [{"id": 456}],
                    }
                ).encode()

            def close(self) -> None:
                pass

        class FailFirstTokenOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, request, timeout):  # type: ignore[no-untyped-def]
                self.calls += 1
                if self.calls == 1:
                    raise http.client.BadStatusLine(marker)
                return TokenResponse()

        with tempfile.TemporaryDirectory(prefix="ab-") as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o700)
            policy = BrokerPolicy.create(
                project_id="agent-container",
                repository="jj1xgo/agent-container",
                default_branch="main",
                protected_branches=("main",),
            )
            session = BrokerSession.create(state, policy)
            tokens = InstallationTokenProvider(
                GitHubAppMetadata(
                    client_id="Iv1abcdefghijk",
                    installation_id=123,
                    repository_id=456,
                    private_key=Path(temporary) / "unused-key.pem",
                ),
                signer=FakeSigner(),
                transport=github_transport,
                clock=lambda: datetime(
                    2026, 8, 28, 13, 0, tzinfo=timezone.utc
                ).timestamp(),
            )
            issue = GitHubIssueTransport(
                policy,
                tokens,
                transport=lambda *_: type(
                    "IssueResponse",
                    (),
                    {
                        "status": 200,
                        "headers": {"Content-Type": "application/json"},
                        "body": b"[]",
                    },
                )(),
            )
            runtime = UploadPackBrokerRuntime(  # type: ignore[arg-type]
                session,
                FakeUploadPackTransport(),
                FakeReceivePackTransport(),
                FakePullRequestTransport(),
                issue,
            )
            environment = {
                "AGENT_BROKER_SOCKET": str(session.socket_path),
                "AGENT_BROKER_CAPABILITY": str(session.capability_path),
                "AGENT_PROJECT_ID": "agent-container",
            }
            opener = FailFirstTokenOpener()

            with mock.patch(
                "agent_container.github_app.urllib.request.build_opener",
                return_value=opener,
            ):
                with runtime:
                    with self.assertRaises(RuntimeError) as raised:
                        request_github_operation("issue-list", {}, environment)
                    self.assertEqual(
                        str(raised.exception),
                        "GitHub broker request was denied",
                    )
                    self.assertNotIn(marker, str(raised.exception))
                    self.assertEqual(
                        request_github_operation("issue-list", {}, environment),
                        {"issues": []},
                    )

            records = [
                json.loads(line)
                for line in session.audit_file.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(records[0]["stage"], "token")
            self.assertEqual(records[1]["status"], "ok")
            self.assertNotIn(marker, session.audit_file.read_text(encoding="utf-8"))

    def test_known_connection_failure_does_not_stop_runtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ab-") as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o700)
            policy = BrokerPolicy.create(
                project_id="agent-container",
                repository="jj1xgo/agent-container",
                default_branch="main",
                protected_branches=("main",),
            )
            session = BrokerSession.create(state, policy)
            upload = FailFirstUploadPackTransport()
            runtime = UploadPackBrokerRuntime(  # type: ignore[arg-type]
                session,
                upload,
                FakeReceivePackTransport(),
                FakePullRequestTransport(),
            )

            with runtime:
                first = BrokerUploadPackClient(
                    session.socket_path,
                    session.capability_path,
                    "agent-container",
                    "jj1xgo/agent-container",
                )
                try:
                    with self.assertRaises(RuntimeError):
                        first.discover()
                finally:
                    first.close()

                second = BrokerUploadPackClient(
                    session.socket_path,
                    session.capability_path,
                    "agent-container",
                    "jj1xgo/agent-container",
                )
                try:
                    self.assertEqual(second.discover(), b"000eversion 2\n0000")
                finally:
                    second.close()

            self.assertEqual(upload.discovery_calls, 2)
            records = [
                json.loads(line)
                for line in session.audit_file.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(records[0]["status"], "error")
            self.assertEqual(records[0]["stage"], "upload-discovery")
            self.assertEqual(records[1]["status"], "ok")

    def test_remote_helper_crosses_real_socket_and_cleans_runtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ab-") as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o700)
            policy = BrokerPolicy.create(
                project_id="agent-container",
                repository="jj1xgo/agent-container",
                default_branch="main",
                protected_branches=("main",),
            )
            session = BrokerSession.create(state, policy)
            run_dir = session.run_dir
            upload = FakeUploadPackTransport()
            receive = FakeReceivePackTransport()
            runtime = UploadPackBrokerRuntime(  # type: ignore[arg-type]
                session,
                upload,
                receive,
                FakePullRequestTransport(),
                FakeIssueTransport(),
            )
            first = b"0009done\n0000"
            second = b"000cls-refs\n0000"
            stdin = BytesIO(
                b"capabilities\n"
                b"connect git-upload-pack\n"
                + first
                + second
            )
            stdout = BytesIO()
            push = (
                pkt(
                    f"{OLD} {NEW} refs/heads/feat/work".encode()
                    + b"\0report-status side-band-64k object-format=sha1\n"
                )
                + b"0000PACKpayload"
            )
            push_stdout = BytesIO()

            with runtime:
                client = BrokerUploadPackClient(
                    session.socket_path,
                    session.capability_path,
                    "agent-container",
                    "jj1xgo/agent-container",
                )
                try:
                    result = run_remote_helper(
                        ["origin", "agent-broker://jj1xgo/agent-container"],
                        {"AGENT_BROKER_REPOSITORY": "jj1xgo/agent-container"},
                        client,
                        stdin,
                        stdout,
                    )
                finally:
                    client.close()
                push_client = BrokerReceivePackClient(
                    session.socket_path,
                    session.capability_path,
                    "agent-container",
                    "jj1xgo/agent-container",
                )
                try:
                    push_result = run_remote_helper(
                        ["origin", "agent-broker://jj1xgo/agent-container"],
                        {"AGENT_BROKER_REPOSITORY": "jj1xgo/agent-container"},
                        push_client,
                        BytesIO(
                            b"capabilities\n"
                            b"connect git-receive-pack\n"
                            + push
                        ),
                        push_stdout,
                    )
                finally:
                    push_client.close()
                environment = {
                    "AGENT_BROKER_SOCKET": str(session.socket_path),
                    "AGENT_BROKER_CAPABILITY": str(session.capability_path),
                    "AGENT_PROJECT_ID": "agent-container",
                }
                capability = session.capability_path.read_text(encoding="ascii").strip()
                pr_result = request_github_operation(
                    "pr-view",
                    {"number": 12},
                    environment,
                )
                issue_list = request_github_operation(
                    "issue-list", {}, environment
                )
                issue_view = request_github_operation(
                    "issue-view", {"number": 12}, environment
                )

            self.assertEqual(result, 0)
            self.assertEqual(push_result, 0)
            self.assertEqual(pr_result["number"], 12)
            self.assertEqual(issue_list, {"issues": [issue_summary()]})
            self.assertEqual(
                issue_view,
                issue_summary() | {"body": "content-body-marker"},
            )
            self.assertEqual(upload.requests, [first, second])
            self.assertEqual(receive.requests, [push])
            self.assertEqual(
                stdout.getvalue(),
                b"connect\nstateless-connect\n\n"
                b"\n"
                b"000eversion 2\n0000"
                b"0008NAK\n0000"
                b"0008NAK\n0000",
            )
            self.assertEqual(
                push_stdout.getvalue(),
                b"connect\nstateless-connect\n\n\n"
                + receive.advertisement
                + b"000eunpack ok\n0000",
            )
            self.assertFalse(run_dir.exists())
            self.assertFalse(session.socket_path.exists())
            self.assertFalse(session.capability_path.exists())
            with self.assertRaises(ValueError):
                request_github_operation("issue-list", {}, environment)
            records = [
                json.loads(line)
                for line in session.audit_file.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 5)
            self.assertEqual(records[0]["status"], "ok")
            self.assertEqual(records[0]["operation"], "git-upload-pack")
            self.assertEqual(records[1]["operation"], "git-receive-pack")
            self.assertEqual(records[1]["ref"], "refs/heads/feat/work")
            self.assertEqual(records[2]["operation"], "pr-view")
            self.assertEqual(records[2]["pr_number"], 12)
            self.assertEqual(records[3]["operation"], "issue-list")
            self.assertNotIn("issue_number", records[3])
            self.assertEqual(records[4]["operation"], "issue-view")
            self.assertEqual(records[4]["issue_number"], 12)
            self.assertTrue(all("capability" not in record for record in records))
            audit = session.audit_file.read_text(encoding="utf-8")
            self.assertNotIn(capability, audit)
            for sentinel in (
                "content-title-marker",
                "content-author-marker",
                "content-label-marker",
                "content-body-marker",
            ):
                self.assertNotIn(sentinel, audit)


if __name__ == "__main__":
    unittest.main()
