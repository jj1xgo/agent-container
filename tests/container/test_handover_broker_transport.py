from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from agent_container.handover_broker import HandoverBrokerSession
from agent_container.handover_broker_protocol import HandoverRequest
from agent_container.handover_broker_protocol import MAX_REQUEST_BYTES
from agent_container.handover_broker_protocol import encode_request_frame
from agent_container.handover_broker_protocol import read_response_frame
from agent_container.handover_broker_transport import handle_handover_connection


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


class Duplex:
    def __init__(
        self,
        incoming: bytes,
        *,
        fail_write: bool = False,
        max_write: int | None = None,
        write_progress: int | None = None,
    ) -> None:
        self.incoming = BytesIO(incoming)
        self.outgoing = BytesIO()
        self.fail_write = fail_write
        self.max_write = max_write
        self.write_progress = write_progress
        self.write_calls = 0
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self.incoming.read(size)

    def write(self, body: bytes) -> int | None:
        self.write_calls += 1
        if self.fail_write:
            raise BrokenPipeError("response-disconnect-marker")
        if self.write_progress is not None:
            return self.write_progress
        length = min(len(body), self.max_write or len(body))
        return self.outgoing.write(body[:length])

    def flush(self) -> None:
        if self.fail_write:
            raise BrokenPipeError("response-disconnect-marker")

    def close(self) -> None:
        self.closed = True


class HandoverBrokerTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.state_root = root / "state"
        self.state_root.mkdir(mode=0o700)
        self.handover_root = root / "handovers"
        self.handover_root.mkdir(mode=0o700)
        self.project_dir = self.handover_root / "agent-container"
        self.project_dir.mkdir(mode=0o700)
        self.session = HandoverBrokerSession.create(
            self.state_root,
            "agent-container",
            self.project_dir.resolve(),
        )
        self.now = datetime(2026, 8, 27, 12, 34, 56, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.session.close()
        self.temporary.cleanup()

    def request(self, **changes: object) -> HandoverRequest:
        request = HandoverRequest(
            version=1,
            capability=self.session._capability,
            project_id="agent-container",
            operation="create",
            title="Safe title",
            body=VALID_BODY,
        )
        return replace(request, **changes)

    def audit_records(self) -> list[dict[str, str]]:
        return [
            json.loads(line)
            for line in self.session.audit_file.read_text(encoding="utf-8").splitlines()
        ]

    def final_files(self) -> list[Path]:
        return sorted(self.project_dir.glob("*.md"))

    def test_valid_request_writes_one_bound_project_file_and_audits_ok(self) -> None:
        first = encode_request_frame(self.request())
        connection = Duplex(first + first)

        with mock.patch(
            "agent_container.handover_broker_transport.create_atomic_handover",
            wraps=__import__(
                "agent_container.handover_writer", fromlist=["create_atomic_handover"]
            ).create_atomic_handover,
        ) as writer:
            result = handle_handover_connection(
                self.session,
                connection,
                os.getuid(),
                now=self.now,
            )

        self.assertEqual(result, 0)
        self.assertTrue(connection.closed)
        response = read_response_frame(BytesIO(connection.outgoing.getvalue()))
        files = self.final_files()
        self.assertEqual(len(files), 1)
        self.assertEqual(response.status, "ok")
        self.assertEqual(response.code, "")
        self.assertEqual(
            response.path,
            f"/handovers/agent-container/{files[0].name}",
        )
        writer.assert_called_once_with(
            self.session.project_dir,
            "agent-container",
            "Safe title",
            VALID_BODY,
            now=self.now,
        )
        self.assertEqual(
            [
                {key: record[key] for key in ("status", "stage", "path")}
                for record in self.audit_records()
            ],
            [{"status": "ok", "stage": "write", "path": response.path}],
        )

    def test_fixed_failures_create_no_file_and_leak_no_request_or_error_text(self) -> None:
        capability_marker = "C" * 43
        title_marker = "private-title-marker"
        body_marker = "private-body-marker"
        base = self.request(
            title=title_marker,
            body=VALID_BODY.replace(
                "## 作業の目的\n目的\n",
                "## 作業の目的\n" + body_marker + "\n",
                1,
            ),
        )
        malformed = b'{"private-body-marker":'
        cases = (
            (
                "authentication",
                encode_request_frame(replace(base, capability=capability_marker)),
                os.getuid(),
                "denied",
                None,
            ),
            (
                "schema",
                struct.pack(">I", len(malformed)) + malformed,
                os.getuid(),
                "denied",
                None,
            ),
            (
                "size",
                struct.pack(">I", MAX_REQUEST_BYTES + 1),
                os.getuid(),
                "denied",
                None,
            ),
            (
                "content-policy",
                encode_request_frame(replace(base, title=title_marker + "\nsecond")),
                os.getuid(),
                "denied",
                None,
            ),
            (
                "filesystem-boundary",
                encode_request_frame(base),
                os.getuid(),
                "error",
                ValueError("filesystem-private-marker"),
            ),
            (
                "write",
                encode_request_frame(base),
                os.getuid(),
                "error",
                OSError("writer-private-marker"),
            ),
            (
                "unavailable",
                encode_request_frame(base),
                os.getuid(),
                "error",
                RuntimeError("unavailable-private-marker"),
            ),
        )

        for stage, frame, peer_uid, status, writer_error in cases:
            with self.subTest(stage=stage):
                self.session.audit_file.write_text("", encoding="utf-8")
                connection = Duplex(frame)
                stdout, stderr = StringIO(), StringIO()
                patcher = (
                    mock.patch(
                        "agent_container.handover_broker_transport.create_atomic_handover",
                        side_effect=writer_error,
                    )
                    if writer_error is not None
                    else mock.patch(
                        "agent_container.handover_broker_transport.create_atomic_handover",
                        wraps=__import__(
                            "agent_container.handover_writer",
                            fromlist=["create_atomic_handover"],
                        ).create_atomic_handover,
                    )
                )
                with patcher as writer, redirect_stdout(stdout), redirect_stderr(stderr):
                    result = handle_handover_connection(
                        self.session,
                        connection,
                        peer_uid,
                        now=self.now,
                    )

                self.assertEqual(result, 1)
                self.assertTrue(connection.closed)
                response = read_response_frame(BytesIO(connection.outgoing.getvalue()))
                self.assertEqual(response.status, status)
                self.assertEqual(response.code, stage)
                self.assertEqual(response.path, "")
                self.assertEqual(self.final_files(), [])
                if stage in {"authentication", "schema", "size", "content-policy"}:
                    writer.assert_not_called()
                records = self.audit_records()
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["status"], status)
                self.assertEqual(records[0]["stage"], stage)
                leaked = (
                    connection.outgoing.getvalue().decode("utf-8", errors="ignore")
                    + self.session.audit_file.read_text(encoding="utf-8")
                    + stdout.getvalue()
                    + stderr.getvalue()
                )
                for marker in (
                    capability_marker,
                    title_marker,
                    body_marker,
                    "filesystem-private-marker",
                    "writer-private-marker",
                    "unavailable-private-marker",
                ):
                    self.assertNotIn(marker, leaked)

    def test_wrong_peer_is_denied_before_content_policy(self) -> None:
        connection = Duplex(
            encode_request_frame(
                self.request(
                    capability="W" * 43,
                    title="private-title-marker\nsecond",
                )
            )
        )

        result = handle_handover_connection(
            self.session,
            connection,
            os.getuid() + 1,
            now=self.now,
        )

        response = read_response_frame(BytesIO(connection.outgoing.getvalue()))
        self.assertEqual(result, 1)
        self.assertEqual(response.status, "denied")
        self.assertEqual(response.code, "authentication")
        self.assertEqual(self.audit_records()[0]["stage"], "authentication")

    def test_response_disconnect_records_only_fixed_response_stage(self) -> None:
        connection = Duplex(encode_request_frame(self.request()), fail_write=True)

        result = handle_handover_connection(
            self.session,
            connection,
            os.getuid(),
            now=self.now,
        )

        self.assertEqual(result, 1)
        self.assertTrue(connection.closed)
        records = self.audit_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "error")
        self.assertEqual(records[0]["stage"], "response")
        audit = self.session.audit_file.read_text(encoding="utf-8")
        self.assertNotIn("response-disconnect-marker", audit)

    def test_response_write_retries_until_the_entire_frame_is_sent(self) -> None:
        connection = Duplex(
            encode_request_frame(self.request()),
            max_write=3,
        )

        result = handle_handover_connection(
            self.session,
            connection,
            os.getuid(),
            now=self.now,
        )

        response = read_response_frame(BytesIO(connection.outgoing.getvalue()))
        self.assertEqual(result, 0)
        self.assertEqual(response.status, "ok")
        self.assertGreater(connection.write_calls, 1)
        self.assertEqual(self.audit_records()[0]["status"], "ok")

    def test_response_write_rejects_invalid_progress(self) -> None:
        created = self.project_dir / "2026-08-27_123456_abcdef12.md"
        for progress in (None, 0, -1):
            with self.subTest(progress=progress):
                self.session.audit_file.write_text("", encoding="utf-8")
                connection = Duplex(
                    encode_request_frame(self.request()),
                    write_progress=progress,
                )
                if progress is None:
                    connection.write = mock.Mock(return_value=None)
                with mock.patch(
                    "agent_container.handover_broker_transport.create_atomic_handover",
                    return_value=created,
                ):
                    result = handle_handover_connection(
                        self.session,
                        connection,
                        os.getuid(),
                        now=self.now,
                    )

                self.assertEqual(result, 1)
                self.assertTrue(connection.closed)
                records = self.audit_records()
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["status"], "error")
                self.assertEqual(records[0]["stage"], "response")


if __name__ == "__main__":
    unittest.main()
