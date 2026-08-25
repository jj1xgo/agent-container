import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest import mock

from agent_container.github_broker import BrokerSession
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_broker_protocol import BrokerRequest


class BrokerSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "state"
        self.root.mkdir(mode=0o700)
        self.policy = BrokerPolicy.create(
            project_id="agent-container",
            repository="jj1xgo/agent-container",
            default_branch="main",
            protected_branches=("main",),
        )
        self.session = BrokerSession.create(self.root, self.policy)

    def tearDown(self) -> None:
        if not self.session._closed:
            self.session.close()
        self.temporary.cleanup()

    def request(self, **changes: object) -> BrokerRequest:
        baseline = {
            "version": 1,
            "capability": self.session._capability,
            "project_id": "agent-container",
            "sequence": self.session._next_sequence,
            "operation": "pr-view",
            "payload": {"number": 1},
        }
        return BrokerRequest(**(baseline | changes))  # type: ignore[arg-type]

    def test_creates_private_project_scoped_runtime(self) -> None:
        self.assertEqual(stat.S_IMODE(self.session.run_dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(self.session.capability_path.stat().st_mode), 0o600
        )
        self.assertEqual(self.session.run_dir.parent.name, "agent-container")
        self.assertFalse(self.session.socket_path.exists())
        capability = self.session.capability_path.read_text(encoding="utf-8").strip()
        self.assertEqual(len(capability), 43)
        self.assertEqual(capability, self.session._capability)

    @mock.patch("agent_container.github_broker.socket.socket")
    def test_rejects_overlong_socket_path_before_socket_creation(
        self, socket_factory: mock.Mock
    ) -> None:
        self.session.socket_path = Path("/" + "x" * 108)
        with self.assertRaisesRegex(ValueError, "too long"):
            self.session.open_listener()
        socket_factory.assert_not_called()

    def test_authorizes_exact_version_capability_project_sequence_and_operation(self) -> None:
        authorized = self.session.authorize(self.request())
        self.assertEqual(authorized["operation"], "pr-view")
        self.assertEqual(authorized["payload"], {"number": 1})
        self.assertEqual(self.session._next_sequence, 2)

    def test_rejects_mismatch_replay_and_closed_session_without_secret_echo(self) -> None:
        marker = "secret-capability-marker"
        cases = (
            self.request(version=2),
            self.request(capability=marker),
            self.request(project_id="other"),
            self.request(sequence=2),
            self.request(operation="merge"),
        )
        for request in cases:
            with self.subTest(request=request.operation):
                with self.assertRaises(ValueError) as raised:
                    self.session.authorize(request)
                self.assertNotIn(marker, str(raised.exception))

        accepted = self.request()
        self.session.authorize(accepted)
        with self.assertRaisesRegex(ValueError, "sequence"):
            self.session.authorize(accepted)
        self.session.close()
        with self.assertRaisesRegex(ValueError, "closed"):
            self.session.authorize(self.request())

    @mock.patch("agent_container.github_broker.os.chmod")
    @mock.patch("agent_container.github_broker.socket.socket")
    def test_opens_private_unix_socket_and_cleans_runtime(
        self, socket_factory: mock.Mock, chmod: mock.Mock
    ) -> None:
        listener = socket_factory.return_value
        returned = self.session.open_listener()
        self.assertIs(returned, listener)
        socket_factory.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind.assert_called_once_with(str(self.session.socket_path))
        listener.listen.assert_called_once_with(4)
        chmod.assert_called_once_with(self.session.socket_path, 0o600)

        run_dir = self.session.run_dir
        self.session.close()
        listener.close.assert_called_once_with()
        self.assertFalse(run_dir.exists())
        self.assertEqual(self.session._capability, "")

    @mock.patch("agent_container.github_broker.os.chmod")
    @mock.patch("agent_container.github_broker.socket.socket")
    def test_listener_rejects_existing_path_and_double_open(
        self, socket_factory: mock.Mock, _: mock.Mock
    ) -> None:
        self.session.socket_path.write_text("replacement", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.session.open_listener()
        self.session.socket_path.unlink()
        self.session.open_listener()
        with self.assertRaises(ValueError):
            self.session.open_listener()
        socket_factory.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)

    def test_real_unix_socket_when_environment_permits_it(self) -> None:
        try:
            listener = self.session.open_listener()
        except PermissionError:
            self.skipTest("Unix socket bind is denied by the execution sandbox")
        self.assertTrue(stat.S_ISSOCK(self.session.socket_path.stat().st_mode))
        self.assertEqual(stat.S_IMODE(self.session.socket_path.stat().st_mode), 0o600)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(self.session.socket_path))
            connection, _ = listener.accept()
            connection.close()
        finally:
            client.close()

    def test_audit_contains_only_allowlisted_metadata(self) -> None:
        capability = self.session._capability
        self.session.audit(
            operation="git-receive-pack",
            status="ok",
            ref="refs/heads/feat/broker",
            bytes_transferred=123,
        )
        self.session.audit(operation="pr-view", status="denied", pr_number=12)

        body = self.session.audit_file.read_text(encoding="utf-8")
        records = [json.loads(line) for line in body.splitlines()]
        self.assertEqual(records[0]["ref"], "refs/heads/feat/broker")
        self.assertEqual(records[0]["bytes"], 123)
        self.assertEqual(records[1]["pr_number"], 12)
        self.assertNotIn(capability, body)
        self.assertNotIn(self.session.run_id, body)
        self.assertEqual(stat.S_IMODE(self.session.audit_file.stat().st_mode), 0o600)

    def test_audit_rejects_unvalidated_metadata_without_writing(self) -> None:
        cases = (
            {"operation": "merge", "status": "ok"},
            {"operation": "pr-view", "status": "secret-marker"},
            {"operation": "git-receive-pack", "status": "ok", "ref": "refs/heads/main"},
            {"operation": "pr-view", "status": "ok", "pr_number": 0},
            {"operation": "pr-view", "status": "ok", "bytes_transferred": -1},
        )
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    self.session.audit(**values)  # type: ignore[arg-type]
        self.assertFalse(self.session.audit_file.exists())

    def test_rejects_unsafe_state_root(self) -> None:
        self.session.close()
        os.chmod(self.root, 0o755)
        with self.assertRaises(PermissionError):
            BrokerSession.create(self.root, self.policy)
