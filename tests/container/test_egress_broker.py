from datetime import datetime
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest import mock

from agent_container.egress_broker import EgressBrokerSession
from agent_container.egress_broker_protocol import EgressRequest
from agent_container.egress_policy import EgressPolicy
from agent_container.state import StateLayout


class EgressBrokerSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "state"
        self.root.mkdir(mode=0o700)
        self.layout = StateLayout(self.root, "demo")
        self.policy = EgressPolicy(1, "allowlist", ("extra.example.com",))
        self.session = EgressBrokerSession.create(self.layout, "codex", self.policy)

    def tearDown(self) -> None:
        self.session.close()
        self.temporary.cleanup()

    def request(self, **changes: object) -> EgressRequest:
        baseline = {
            "version": 1,
            "capability": self.session._capability,
            "project_id": "demo",
            "sequence": 1,
            "operation": "connect",
            "domain": "extra.example.com",
            "port": 443,
        }
        return EgressRequest(**(baseline | changes))  # type: ignore[arg-type]

    def test_creates_private_project_scoped_runtime(self) -> None:
        self.assertEqual(stat.S_IMODE(self.session.run_dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(self.session.capability_path.stat().st_mode), 0o400
        )
        self.assertEqual(self.session.run_dir.parent, self.layout.egress_broker_run_root)
        self.assertLessEqual(len(os.fsencode(self.session.socket_path)), 107)
        self.assertEqual(
            self.session.capability_path.read_text(encoding="ascii").strip(),
            self.session._capability,
        )

    def test_authorizes_exact_monotonic_request_and_managed_union(self) -> None:
        self.assertEqual(
            self.session.authorize(self.request(), os.getuid()),
            "extra.example.com",
        )
        with self.assertRaisesRegex(ValueError, "sequence"):
            self.session.authorize(self.request(sequence=1), os.getuid())

        with mock.patch.dict(
            "agent_container.egress_broker.MANAGED_EGRESS_DOMAINS",
            {"codex": frozenset({"managed.example.com"})},
            clear=True,
        ):
            managed = EgressBrokerSession.create(self.layout, "codex", self.policy)
        try:
            self.assertEqual(
                managed.authorize(
                    self.request(
                        capability=managed._capability,
                        sequence=1,
                        domain="managed.example.com",
                    ),
                    os.getuid(),
                ),
                "managed.example.com",
            )
        finally:
            managed.close()

    def test_rejects_auth_policy_replay_and_closed_session_without_echo(self) -> None:
        marker = "private-capability-marker"
        cases = (
            (self.request(version=2), os.getuid()),
            (self.request(capability=marker), os.getuid()),
            (self.request(project_id="other"), os.getuid()),
            (self.request(operation="resolve"), os.getuid()),
            (self.request(port=80), os.getuid()),
            (self.request(domain="denied.example.com"), os.getuid()),
            (self.request(), os.getuid() + 1),
        )
        for request, peer_uid in cases:
            with self.subTest(request=request), self.assertRaises(ValueError) as raised:
                self.session.authorize(request, peer_uid)
            self.assertNotIn(marker, str(raised.exception))

        self.session.deactivate()
        with self.assertRaisesRegex(ValueError, "closed"):
            self.session.authorize(self.request(), os.getuid())

    @mock.patch("agent_container.egress_broker.os.chmod")
    @mock.patch("agent_container.egress_broker.socket.socket")
    def test_listener_is_single_use_and_close_removes_exact_runtime(
        self, socket_factory: mock.Mock, chmod: mock.Mock
    ) -> None:
        listener = self.session.open_listener()
        self.assertIs(listener, socket_factory.return_value)
        socket_factory.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind.assert_called_once_with(str(self.session.socket_path))
        chmod.assert_called_once_with(self.session.socket_path, 0o600)
        with self.assertRaises(ValueError):
            self.session.open_listener()

        run_dir = self.session.run_dir
        self.session.close()
        listener.close.assert_called_once_with()
        self.assertFalse(run_dir.exists())
        self.assertEqual(self.session._capability, "")

    @mock.patch("agent_container.egress_broker.socket.socket")
    def test_rejects_overlong_socket_path_before_socket_creation(
        self, socket_factory: mock.Mock
    ) -> None:
        self.session.socket_path = Path("/" + "x" * 108)
        with self.assertRaisesRegex(ValueError, "too long"):
            self.session.open_listener()
        socket_factory.assert_not_called()

    def test_close_refuses_replaced_capability_path(self) -> None:
        run_dir = self.session.run_dir
        self.session.capability_path.unlink()
        self.session.capability_path.mkdir()

        with self.assertRaisesRegex(ValueError, "cleanup failed"):
            self.session.close()

        self.assertTrue(self.session.capability_path.is_dir())
        self.assertTrue(run_dir.exists())
        self.session.capability_path.rmdir()
        self.session.close()

    def test_create_rejects_symlinked_or_broad_audit_file(self) -> None:
        self.session.close()
        audit_file = self.layout.egress_broker_audit_file
        audit_file.unlink()
        target = Path(self.temporary.name) / "audit-target"
        target.write_text("", encoding="ascii")
        target.chmod(0o600)
        audit_file.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "regular non-symlink"):
            EgressBrokerSession.create(self.layout, "codex", self.policy)

        audit_file.unlink()
        audit_file.write_text("", encoding="ascii")
        audit_file.chmod(0o644)
        with self.assertRaisesRegex(PermissionError, "mode 0600"):
            EgressBrokerSession.create(self.layout, "codex", self.policy)

    def test_audit_contains_only_fixed_secret_free_metadata(self) -> None:
        markers = (
            self.session._capability,
            "private.example.com",
            "192.0.2.10",
            "private-exception-marker",
        )
        self.session.audit("ok", bytes_from_client=12, bytes_from_upstream=34)
        self.session.audit("denied", stage="policy")
        records = [
            json.loads(line)
            for line in self.session.audit_file.read_text(encoding="ascii").splitlines()
        ]

        self.assertEqual(
            set(records[0]),
            {
                "timestamp",
                "run",
                "project",
                "agent",
                "operation",
                "status",
                "bytes_from_client",
                "bytes_from_upstream",
            },
        )
        self.assertEqual(records[0]["agent"], "codex")
        self.assertEqual(records[1]["stage"], "policy")
        datetime.fromisoformat(records[0]["timestamp"])
        body = self.session.audit_file.read_text(encoding="ascii")
        for marker in markers:
            self.assertNotIn(marker, body)

    def test_audit_rejects_unbounded_or_unknown_values(self) -> None:
        cases = (
            {"status": "private"},
            {"status": "error", "stage": "private"},
            {"status": "ok", "stage": "relay"},
            {"status": "ok", "bytes_from_client": -1},
            {"status": "ok", "bytes_from_upstream": (1 << 31) + 1},
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.session.audit(**values)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
