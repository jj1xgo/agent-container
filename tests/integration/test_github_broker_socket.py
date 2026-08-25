from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import unittest

from agent_container.git_remote_helper import run_remote_helper
from agent_container.github_broker import BrokerSession
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_broker_runtime import UploadPackBrokerRuntime
from agent_container.github_broker_transport import BrokerUploadPackClient
from agent_container.github_broker_transport import BrokerReceivePackClient


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


@unittest.skipUnless(
    RUN_SOCKET_INTEGRATION,
    "set AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 for Unix socket integration",
)
class GitHubBrokerSocketIntegrationTest(unittest.TestCase):
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
                session, upload, receive
            )
            first = b"0009done\n0000"
            second = b"000cls-refs\n0000"
            stdin = BytesIO(
                b"capabilities\n"
                b"stateless-connect git-upload-pack\n"
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
                            b"stateless-connect git-receive-pack\n"
                            + push
                        ),
                        push_stdout,
                    )
                finally:
                    push_client.close()

            self.assertEqual(result, 0)
            self.assertEqual(push_result, 0)
            self.assertEqual(upload.requests, [first, second])
            self.assertEqual(receive.requests, [push])
            self.assertEqual(
                stdout.getvalue(),
                b"stateless-connect\n\n"
                b"\n"
                b"000eversion 2\n0000"
                b"0008NAK\n0002"
                b"0008NAK\n0002",
            )
            self.assertEqual(
                push_stdout.getvalue(),
                b"stateless-connect\n\n\n"
                + receive.advertisement
                + b"000eunpack ok\n0000",
            )
            self.assertFalse(run_dir.exists())
            records = [
                json.loads(line)
                for line in session.audit_file.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["status"], "ok")
            self.assertEqual(records[0]["operation"], "git-upload-pack")
            self.assertEqual(records[1]["operation"], "git-receive-pack")
            self.assertEqual(records[1]["ref"], "refs/heads/feat/work")
            self.assertTrue(all("capability" not in record for record in records))


if __name__ == "__main__":
    unittest.main()
