from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import re
import socket
import stat
import tempfile
import unittest
from unittest import mock

from agent_container.handover_broker import HandoverBrokerSession
from agent_container.handover_broker_protocol import HandoverRequest


VALID_BODY = """## 作業の目的
目的
## 現在地
現在地
## 決定事項と理由
決定
## 変更したファイル・commit・PR
変更
## 検証結果
検証
## 未解決事項とリスク
リスク
## 次の一手
次
"""


class HandoverBrokerSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        temporary_root = Path(self.temporary.name)
        self.state_root = temporary_root / "state"
        self.state_root.mkdir(mode=0o700)
        self.handover_root = temporary_root / "handovers"
        self.handover_root.mkdir(mode=0o700)
        self.project_dir = self.handover_root / "agent-container"
        self.project_dir.mkdir(mode=0o700)
        self.session = HandoverBrokerSession.create(
            self.state_root,
            "agent-container",
            self.project_dir.resolve(),
        )

    def tearDown(self) -> None:
        if not self.session._cleanup_complete:
            self.session.close()
        self.temporary.cleanup()

    def request(self, **changes: object) -> HandoverRequest:
        request = HandoverRequest(
            version=1,
            capability=self.session._capability,
            project_id="agent-container",
            operation="create",
            title=" Safe title ",
            body=VALID_BODY.rstrip("\n"),
        )
        return replace(request, **changes)

    def test_creates_private_project_scoped_runtime_and_persistent_audit(self) -> None:
        capability = self.session.capability_path.read_text(encoding="utf-8").strip()

        self.assertEqual(self.session.project_dir, self.project_dir.resolve())
        self.assertEqual(stat.S_IMODE(self.session.run_dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(self.session.capability_path.stat().st_mode), 0o600
        )
        self.assertEqual(self.session.run_dir.parent.name, "1f630d4dd972")
        self.assertEqual(len(capability), 43)
        self.assertRegex(capability, re.compile(r"^[A-Za-z0-9_-]{43}$"))
        self.assertEqual(capability, self.session._capability)
        self.assertEqual(
            self.session.audit_file,
            self.state_root / "handover-broker/audit/events.jsonl",
        )
        self.assertFalse(self.session.audit_file.is_relative_to(self.session.run_dir))
        self.assertEqual(stat.S_IMODE(self.session.audit_file.stat().st_mode), 0o600)
        self.assertEqual(self.session.audit_file.read_text(encoding="utf-8"), "")

    def test_authorizes_exact_session_and_returns_validated_content(self) -> None:
        title, body = self.session.authorize(self.request(), os.getuid())

        self.assertEqual(title, "Safe title")
        self.assertEqual(body, VALID_BODY)

    def test_deactivate_denies_new_authorization_and_publication(self) -> None:
        request = self.request()
        self.session.deactivate()

        with self.assertRaisesRegex(ValueError, "closed"):
            self.session.authorize(request, os.getuid())
        with self.assertRaisesRegex(OSError, "unavailable"):
            with self.session.publication_guard():
                self.fail("a deactivated session granted publication")

    def test_rejects_wrong_uid_before_comparing_capability(self) -> None:
        with mock.patch(
            "agent_container.handover_broker.secrets.compare_digest"
        ) as compare_digest:
            with self.assertRaisesRegex(ValueError, "authorized"):
                self.session.authorize(self.request(), os.getuid() + 1)

        compare_digest.assert_not_called()

    def test_rejects_mismatched_and_closed_requests_without_secret_echo(self) -> None:
        capability_marker = "A" * 43
        cases = (
            replace(self.request(), capability=capability_marker),
            replace(self.request(), version=2),
            replace(self.request(), project_id="other"),
            replace(self.request(), operation="replace"),
        )
        for request in cases:
            with self.subTest(request=request):
                with self.assertRaises(ValueError) as raised:
                    self.session.authorize(request, os.getuid())
                self.assertNotIn(capability_marker, str(raised.exception))

        old_request = self.request()
        self.session.close()
        with self.assertRaisesRegex(ValueError, "closed"):
            self.session.authorize(old_request, os.getuid())
        self.assertNotIn(
            capability_marker,
            self.session.audit_file.read_text(encoding="utf-8"),
        )

    @mock.patch("agent_container.handover_broker.os.chmod")
    @mock.patch("agent_container.handover_broker.socket.socket")
    def test_open_listener_creates_private_socket_and_close_removes_runtime(
        self,
        socket_factory: mock.Mock,
        chmod: mock.Mock,
    ) -> None:
        run_dir = self.session.run_dir
        old_request = self.request()
        listener = self.session.open_listener()

        self.assertIs(listener, socket_factory.return_value)
        socket_factory.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind.assert_called_once_with(str(self.session.socket_path))
        listener.listen.assert_called_once_with(4)
        chmod.assert_called_once_with(self.session.socket_path, 0o600)

        self.session.close()
        self.assertEqual(self.session._capability, "")
        self.assertFalse(run_dir.exists())
        listener.close.assert_called_once_with()
        with self.assertRaisesRegex(ValueError, "closed"):
            self.session.authorize(old_request, os.getuid())

    @mock.patch("agent_container.handover_broker.os.chmod")
    @mock.patch("agent_container.handover_broker.socket.socket")
    def test_listener_rejects_existing_path_and_double_open(
        self,
        socket_factory: mock.Mock,
        _: mock.Mock,
    ) -> None:
        self.session.socket_path.write_text("replacement", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.session.open_listener()
        self.session.socket_path.unlink()
        self.session.open_listener()
        with self.assertRaises(ValueError):
            self.session.open_listener()
        socket_factory.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)

    @mock.patch("agent_container.handover_broker.os.chmod")
    @mock.patch("agent_container.handover_broker.socket.socket")
    def test_close_refuses_replaced_socket_path(
        self,
        socket_factory: mock.Mock,
        _: mock.Mock,
    ) -> None:
        old_request = self.request()
        run_dir = self.session.run_dir
        listener = self.session.open_listener()
        self.session.socket_path.write_text("replacement", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "cleanup failed"):
            self.session.close()

        self.assertIs(listener, socket_factory.return_value)
        listener.close.assert_called_once_with()
        self.assertTrue(self.session.socket_path.is_file())
        self.assertFalse(self.session.capability_path.exists())
        self.assertTrue(run_dir.exists())
        self.assertEqual(self.session._capability, "")
        with self.assertRaisesRegex(ValueError, "closed"):
            self.session.authorize(old_request, os.getuid())

        self.session.socket_path.unlink()
        self.session.close()
        self.assertFalse(run_dir.exists())

    def test_close_refuses_replaced_capability_path(self) -> None:
        old_request = self.request()
        run_dir = self.session.run_dir
        self.session.capability_path.unlink()
        self.session.capability_path.mkdir()

        with self.assertRaisesRegex(ValueError, "cleanup failed"):
            self.session.close()

        self.assertTrue(self.session.capability_path.is_dir())
        self.assertTrue(run_dir.exists())
        self.assertEqual(self.session._capability, "")
        with self.assertRaisesRegex(ValueError, "closed"):
            self.session.authorize(old_request, os.getuid())

        self.session.capability_path.rmdir()
        self.session.close()
        self.assertFalse(run_dir.exists())

    def test_audit_contains_only_allowlisted_secret_free_metadata(self) -> None:
        capability = self.session._capability
        title = "private title marker"
        body_marker = "private body marker"
        body = VALID_BODY.replace(
            "## 作業の目的\n目的\n",
            "## 作業の目的\n" + body_marker + "\n",
            1,
        )
        path = "/handovers/agent-container/2026-08-27_123456_abcdef12.md"

        self.session.authorize(
            self.request(title=title, body=body),
            os.getuid(),
        )
        self.session.audit("ok", stage="write", path=path)
        self.session.audit("denied", stage="authentication")

        audit_body = self.session.audit_file.read_text(encoding="utf-8")
        records = [json.loads(line) for line in audit_body.splitlines()]
        self.assertEqual(
            set(records[0]),
            {"timestamp", "run", "project", "operation", "status", "stage", "path"},
        )
        self.assertEqual(records[0]["run"], self.session.run_label)
        self.assertEqual(records[0]["project"], "agent-container")
        self.assertEqual(records[0]["operation"], "create")
        self.assertEqual(records[0]["status"], "ok")
        self.assertEqual(records[0]["stage"], "write")
        self.assertEqual(records[0]["path"], path)
        self.assertEqual(
            set(records[1]),
            {"timestamp", "run", "project", "operation", "status", "stage"},
        )
        datetime.fromisoformat(records[0]["timestamp"])
        self.assertNotIn(capability, audit_body)
        self.assertNotIn(self.session.run_id, audit_body)
        self.assertNotIn(title, audit_body)
        self.assertNotIn(body_marker, audit_body)
        self.assertEqual(stat.S_IMODE(self.session.audit_file.stat().st_mode), 0o600)

    def test_audit_rejects_unvalidated_metadata_without_writing(self) -> None:
        cases = (
            {"status": "secret-marker", "stage": "write"},
            {"status": "error", "stage": "secret-marker"},
            {"status": "ok", "stage": "write"},
            {
                "status": "ok",
                "stage": "write",
                "path": "/host/private/secret-marker",
            },
            {
                "status": "error",
                "stage": "write",
                "path": "/handovers/agent-container/2026-08-27_123456_abcdef12.md",
            },
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.session.audit(**values)

        self.assertEqual(self.session.audit_file.read_text(encoding="utf-8"), "")

    def test_create_rejects_untrusted_project_directory(self) -> None:
        for project_dir in (
            Path("relative/agent-container"),
            self.handover_root.resolve(),
        ):
            with self.subTest(project_dir=project_dir), self.assertRaises(ValueError):
                HandoverBrokerSession.create(
                    self.state_root,
                    "agent-container",
                    project_dir,
                )

    def test_create_rejects_symlinked_audit_file(self) -> None:
        self.session.close()
        self.session.audit_file.unlink()
        target = Path(self.temporary.name) / "audit-target"
        target.write_text("", encoding="utf-8")
        target.chmod(0o600)
        self.session.audit_file.symlink_to(target)

        with self.assertRaisesRegex(ValueError, "symlink"):
            HandoverBrokerSession.create(
                self.state_root,
                "agent-container",
                self.project_dir.resolve(),
            )

    def test_create_rejects_preexisting_fifo_audit_without_blocking(self) -> None:
        self.session.close()
        self.session.audit_file.unlink()
        os.mkfifo(self.session.audit_file, 0o600)
        real_open = os.open
        observed_flags: list[int] = []

        def require_nonblocking_open(
            path: object, flags: int, *args: object, **kwargs: object
        ) -> int:
            if Path(path) == self.session.audit_file:
                observed_flags.append(flags)
                if not flags & os.O_NONBLOCK:
                    raise AssertionError("audit FIFO open would block")
            return real_open(path, flags, *args, **kwargs)

        with mock.patch(
            "agent_container.handover_broker.os.open",
            side_effect=require_nonblocking_open,
        ):
            with self.assertRaisesRegex(ValueError, "regular non-symlink"):
                HandoverBrokerSession.create(
                    self.state_root,
                    "agent-container",
                    self.project_dir.resolve(),
                )

        self.assertEqual(len(observed_flags), 1)
        self.assertTrue(observed_flags[0] & os.O_NONBLOCK)

    def test_create_rejects_preexisting_audit_with_wrong_mode(self) -> None:
        self.session.close()
        self.session.audit_file.chmod(0o644)

        with self.assertRaisesRegex(PermissionError, "mode 0600"):
            HandoverBrokerSession.create(
                self.state_root,
                "agent-container",
                self.project_dir.resolve(),
            )

    def test_create_rejects_preexisting_audit_with_wrong_owner(self) -> None:
        self.session.close()
        real_fstat = os.fstat

        def foreign_owner(descriptor: int) -> os.stat_result:
            metadata = real_fstat(descriptor)
            values = list(metadata)
            values[4] = os.getuid() + 1
            return os.stat_result(values)

        with mock.patch(
            "agent_container.handover_broker.os.fstat",
            side_effect=foreign_owner,
        ):
            with self.assertRaisesRegex(PermissionError, "current user"):
                HandoverBrokerSession.create(
                    self.state_root,
                    "agent-container",
                    self.project_dir.resolve(),
                )

    def test_rejects_unsafe_state_root(self) -> None:
        self.session.close()
        os.chmod(self.state_root, 0o755)
        with self.assertRaises(PermissionError):
            HandoverBrokerSession.create(
                self.state_root,
                "agent-container",
                self.project_dir.resolve(),
            )
