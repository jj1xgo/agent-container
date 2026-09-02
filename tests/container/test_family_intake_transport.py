from io import BytesIO
import os
from pathlib import Path
import socket
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_container.family_intake_broker import FamilyIntakeInternalError
from agent_container.family_intake_broker import FamilyIntakeSession
from agent_container.family_intake_protocol import decode_response_frame
from agent_container.family_intake_protocol import encode_request_frame
from agent_container.family_intake_protocol import FamilyIntakeRequest
from agent_container.family_intake_transport import handle_family_intake_connection
from agent_container.family_pending import list_pending


NOW = 1_800_000_000
PEER_PID = 4242
CAPABILITY = "c" * 43


class FakeStream:
    def __init__(self, incoming: bytes, *, fail_write: bool = False) -> None:
        self.incoming = BytesIO(incoming)
        self.outgoing = BytesIO()
        self.fail_write = fail_write
        self.closed = False

    def read(self, size: int) -> bytes:
        return self.incoming.read(size)

    def write(self, body: bytes) -> int:
        if self.fail_write:
            raise BrokenPipeError("private-disconnect-marker")
        return self.outgoing.write(body)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, stream: FakeStream, *, pid: int = PEER_PID, uid: int = os.getuid()) -> None:
        self.stream = stream
        self.pid = pid
        self.uid = uid
        self.credential_calls: list[tuple[int, int, int]] = []

    def getsockopt(self, level: int, option: int, size: int) -> bytes:
        self.credential_calls.append((level, option, size))
        return struct.pack("3i", self.pid, self.uid, 9999)

    def makefile(self, *_: object, **__: object) -> FakeStream:
        return self.stream


class FamilyIntakeTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        project = root / "family" / "projects" / "demo"
        self.store = project / "pending"
        self.binding = project / "binding.json"
        self.audit = project / "audit" / "events.jsonl"
        for directory in (
            root / "family",
            root / "family" / "projects",
            project,
            self.store,
            self.audit.parent,
        ):
            directory.mkdir(exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        self.binding.write_text(
            '{"repository":"family/roadmap","repository_id":12345}\n',
            encoding="ascii",
        )
        self.binding.chmod(0o600)

    def request(self, *, capability: str = CAPABILITY) -> FamilyIntakeRequest:
        return FamilyIntakeRequest(
            1,
            "issue_create_request",
            capability,
            {
                "title": "Add export",
                "summary": "Portable copy.",
                "context": "No export exists.",
                "acceptance_criteria": ["JSON downloads"],
            },
        )

    def session(self) -> FamilyIntakeSession:
        session = FamilyIntakeSession(
            "demo",
            CAPABILITY,
            NOW + 60,
            store=self.store,
            binding_path=self.binding,
            audit_path=self.audit,
            agent="codex",
            repository="demo",
            owner_uid=os.getuid(),
            clock=lambda: NOW,
            random_bytes=lambda size: b"\x22" * size,
            process_reader=lambda pid: (
                (1, 100, os.getuid())
                if pid == PEER_PID
                else (_ for _ in ()).throw(ProcessLookupError(pid))
            ),
        )
        session.register_runtime(PEER_PID)
        return session

    # Break caught: transport trusting caller-supplied identity instead of SO_PEERCRED.
    def test_reads_peer_credentials_and_writes_one_exact_response(self) -> None:
        session = self.session()
        stream = FakeStream(encode_request_frame(self.request()))
        connection = FakeConnection(stream)

        handle_family_intake_connection(connection, session, self.store)

        self.assertEqual(
            connection.credential_calls,
            [(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)],
        )
        response, consumed = decode_response_frame(stream.outgoing.getvalue())
        self.assertEqual(consumed, len(stream.outgoing.getvalue()))
        self.assertEqual(
            (response.version, response.status, response.request_id, response.expires_at),
            (1, "pending", "22" * 16, 1_800_086_400),
        )
        self.assertTrue(stream.closed)

    # Break caught: incomplete or malformed frames consuming capability or persisting content.
    def test_disconnect_before_complete_frame_leaves_capability_reusable(self) -> None:
        session = self.session()
        incomplete = encode_request_frame(self.request())[:-3]

        handle_family_intake_connection(
            FakeConnection(FakeStream(incomplete)),
            session,
            self.store,
        )

        self.assertFalse(session.consumed)
        self.assertEqual(list_pending(self.store, "demo"), ())
        valid = FakeStream(encode_request_frame(self.request()))
        handle_family_intake_connection(FakeConnection(valid), session, self.store)
        self.assertEqual(len(list_pending(self.store, "demo")), 1)

    # Break caught: response disconnect rolling back or replaying a durable request.
    def test_disconnect_after_persistence_keeps_one_pending_and_consumes_run(self) -> None:
        session = self.session()
        disconnected = FakeStream(
            encode_request_frame(self.request()),
            fail_write=True,
        )

        handle_family_intake_connection(
            FakeConnection(disconnected),
            session,
            self.store,
        )

        self.assertTrue(session.consumed)
        self.assertEqual(len(list_pending(self.store, "demo")), 1)
        replay = FakeStream(encode_request_frame(self.request()))
        handle_family_intake_connection(FakeConnection(replay), session, self.store)
        self.assertEqual(replay.outgoing.getvalue(), b"")
        self.assertEqual(len(list_pending(self.store, "demo")), 1)

    # Break caught: an internal persistence error being downgraded to a disconnect.
    def test_internal_failure_is_silent_to_client_but_propagates_to_supervisor(self) -> None:
        session = self.session()
        stream = FakeStream(encode_request_frame(self.request()))

        with patch(
            "agent_container.family_intake_broker.create_pending",
            side_effect=OSError("private-transport-marker"),
        ):
            with self.assertRaises(FamilyIntakeInternalError) as raised:
                handle_family_intake_connection(
                    FakeConnection(stream),
                    session,
                    self.store,
                )

        self.assertEqual(str(raised.exception), "family intake persistence failed")
        self.assertNotIn("private-transport-marker", str(raised.exception))
        self.assertNotIn(CAPABILITY, str(raised.exception))
        self.assertEqual(stream.outgoing.getvalue(), b"")
        self.assertTrue(stream.closed)
        self.assertTrue(session.failed)

    # Break caught: a different process consuming the capability before the expected runtime.
    def test_peer_mismatch_is_silent_and_does_not_read_or_consume(self) -> None:
        session = self.session()
        stream = FakeStream(encode_request_frame(self.request()))

        handle_family_intake_connection(
            FakeConnection(stream, pid=PEER_PID + 1),
            session,
            self.store,
        )

        self.assertEqual(stream.incoming.tell(), 0)
        self.assertEqual(stream.outgoing.getvalue(), b"")
        self.assertFalse(session.consumed)

    # Break caught: a valid capability being redirected to another project's pending store.
    def test_rejects_store_mismatch_without_consumption(self) -> None:
        session = self.session()
        other = self.store.parent.parent / "other" / "pending"
        stream = FakeStream(encode_request_frame(self.request()))

        handle_family_intake_connection(FakeConnection(stream), session, other)

        self.assertEqual(stream.outgoing.getvalue(), b"")
        self.assertFalse(session.consumed)


if __name__ == "__main__":
    unittest.main()
