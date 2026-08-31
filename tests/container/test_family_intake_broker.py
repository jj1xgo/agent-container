import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_container.family_intake_broker import FamilyIntakeSession
from agent_container.family_intake_broker import FamilyIntakeDenied
from agent_container.family_intake_broker import FamilyIntakeInternalError
from agent_container.family_intake_protocol import FamilyIntakeRequest
from agent_container.family_intake_protocol import FamilyIntakeResponse
from agent_container.family_issue import CanonicalFamilyIssue
from agent_container.family_pending import create_pending
from agent_container.family_pending import list_pending


NOW = 1_800_000_000
PEER_PID = 4242
CAPABILITY = "c" * 43


class FamilyIntakeBrokerTest(unittest.TestCase):
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

    def request(self, **changes: object) -> FamilyIntakeRequest:
        values = {
            "version": 1,
            "operation": "issue_create_request",
            "capability": CAPABILITY,
            "payload": {
                "title": "Add export",
                "summary": "Portable copy.",
                "context": "No export exists.",
                "acceptance_criteria": ["JSON downloads"],
            },
        }
        values.update(changes)
        return FamilyIntakeRequest(**values)  # type: ignore[arg-type]

    def session(self, **changes: object) -> FamilyIntakeSession:
        register = changes.pop("register", True)
        values = {
            "project_id": "demo",
            "capability": CAPABILITY,
            "expires_at": NOW + 60,
            "store": self.store,
            "binding_path": self.binding,
            "audit_path": self.audit,
            "owner_uid": os.getuid(),
            "clock": lambda: NOW,
            "random_bytes": lambda size: b"\x11" * size,
            "process_reader": lambda pid: (
                (1, 100, os.getuid())
                if pid == PEER_PID
                else (_ for _ in ()).throw(ProcessLookupError(pid))
            ),
        }
        values.update(changes)
        session = FamilyIntakeSession(**values)  # type: ignore[arg-type]
        if register:
            session.register_runtime(PEER_PID)
        return session

    def unarmed_session(self, identities: dict[int, tuple[int, int, int]]):
        def read_process(pid: int) -> tuple[int, int, int]:
            try:
                return identities[pid]
            except KeyError:
                raise ProcessLookupError(pid) from None

        return FamilyIntakeSession(
            "demo",
            CAPABILITY,
            NOW + 60,
            store=self.store,
            binding_path=self.binding,
            audit_path=self.audit,
            owner_uid=os.getuid(),
            clock=lambda: NOW,
            random_bytes=lambda size: b"\x11" * size,
            process_reader=read_process,
        )

    # Break caught: startup guessing a future client PID instead of arming once post-spawn.
    def test_starts_unarmed_then_registers_one_runtime_root_once(self) -> None:
        identities = {400: (1, 100, os.getuid())}
        session = self.unarmed_session(identities)

        with self.assertRaises(ValueError):
            session.validate_peer(400, os.getuid())
        session.register_runtime(400)
        session.validate_peer(400, os.getuid())
        with self.assertRaises(ValueError):
            session.register_runtime(400)

    # Break caught: a sibling process using the same socket/capability as the runtime tree.
    def test_accepts_stable_descendant_chain_and_rejects_unrelated_sibling(self) -> None:
        identities = {
            400: (1, 100, os.getuid()),
            401: (400, 101, os.getuid()),
            402: (401, 102, os.getuid()),
            403: (1, 103, os.getuid()),
        }
        session = self.unarmed_session(identities)
        session.register_runtime(400)

        session.validate_peer(402, os.getuid())
        with self.assertRaises(ValueError):
            session.validate_peer(403, os.getuid())

    # Break caught: dead roots or PID reuse authenticating a new process tree.
    def test_registration_and_peer_validation_reject_dead_or_reused_root(self) -> None:
        identities = {400: (1, 100, os.getuid())}
        dead_identities: dict[int, tuple[int, int, int]] = {}
        dead = self.unarmed_session(dead_identities)
        with self.assertRaises(ValueError):
            dead.register_runtime(400)
        dead_identities[400] = (1, 100, os.getuid())
        with self.assertRaises(ValueError):
            dead.register_runtime(400)
        identities[400] = (1, 200, os.getuid())
        session = self.unarmed_session(identities)
        session.register_runtime(400)
        identities[400] = (1, 201, os.getuid())
        with self.assertRaises(ValueError):
            session.validate_peer(400, os.getuid())

    # Break caught: cycles or an unbounded process tree making authorization non-terminating.
    def test_process_parent_traversal_is_bounded_and_cycle_safe(self) -> None:
        identities = {400: (1, 100, os.getuid())}
        identities.update(
            {
                pid: (pid - 1, pid, os.getuid())
                for pid in range(401, 470)
            }
        )
        session = self.unarmed_session(identities)
        session.register_runtime(400)

        with self.assertRaises(ValueError):
            session.validate_peer(469, os.getuid())
        identities[401] = (402, 101, os.getuid())
        identities[402] = (401, 102, os.getuid())
        with self.assertRaises(ValueError):
            session.validate_peer(402, os.getuid())

    # Break caught: a successful intake exposing content instead of only a receipt.
    def test_persists_canonical_request_and_returns_only_fixed_pending_receipt(self) -> None:
        session = self.session()

        response = session.handle(self.request())

        self.assertEqual(
            response,
            FamilyIntakeResponse(1, "pending", "11" * 16, 1_800_086_400),
        )
        pending = list_pending(self.store)
        self.assertEqual(len(pending), 1)
        self.assertEqual(
            pending[0].issue,
            CanonicalFamilyIssue(
                "Add export",
                "## Summary\n\nPortable copy.\n\n"
                "## Context\n\nNo export exists.\n\n"
                "## Acceptance criteria\n\n- JSON downloads\n",
            ),
        )
        self.assertTrue(session.consumed)
        self.assertEqual(
            json.loads(self.audit.read_text("ascii")),
            {
                "operation": "intake",
                "project_id": "demo",
                "request_id": "11" * 16,
                "stage": "intake",
                "status": "pending",
                "timestamp": NOW,
            },
        )
        receipt_and_audit = repr(response) + self.audit.read_text("ascii")
        for forbidden in (
            CAPABILITY,
            "Add export",
            "Portable copy",
            "No export exists",
            "JSON downloads",
            "repository",
            str(self.store),
        ):
            self.assertNotIn(forbidden, receipt_and_audit)

    # Break caught: direct callers bypassing version, operation, capability, or expiry.
    def test_rejects_wrong_authorization_values_without_consuming_capability(self) -> None:
        cases = (
            (self.session(), self.request(version=2)),
            (self.session(), self.request(operation="issue_preview")),
            (self.session(), self.request(capability="x" * 43)),
            (self.session(expires_at=NOW), self.request()),
        )
        for session, request in cases:
            with self.subTest(request=request, expires_at=session.expires_at):
                with self.assertRaises(ValueError):
                    session.handle(request)
                self.assertFalse(session.consumed)
        self.assertEqual(list_pending(self.store), ())

    # Break caught: malformed content burning the one-time capability before validation.
    def test_complete_schema_is_validated_before_capability_consumption(self) -> None:
        session = self.session()
        malformed = self.request(
            payload={
                "title": "Add export",
                "summary": "Portable copy.",
                "context": "No export exists.",
                "acceptance_criteria": [],
            }
        )

        with self.assertRaises(ValueError):
            session.handle(malformed)

        self.assertFalse(session.consumed)
        self.assertEqual(
            session.handle(self.request()).request_id,
            "11" * 16,
        )

    # Break caught: a reused run capability creating a second pending request.
    def test_one_request_per_run_rejects_reuse(self) -> None:
        session = self.session()
        first = session.handle(self.request())

        with self.assertRaises(ValueError):
            session.handle(self.request())

        self.assertEqual(first.request_id, "11" * 16)
        self.assertEqual(len(list_pending(self.store)), 1)

    # Break caught: intake bypassing the ten-unfinished-request store limit.
    def test_pending_count_limit_consumes_capability_fail_closed(self) -> None:
        issue = CanonicalFamilyIssue("Title", "Body")
        for byte in range(10):
            create_pending(
                self.store,
                "demo",
                issue,
                now=NOW,
                random_bytes=lambda size, byte=byte: bytes([byte]) * size,
            )
        session = self.session()

        with self.assertRaises(ValueError):
            session.handle(self.request())

        self.assertTrue(session.consumed)
        with self.assertRaises(ValueError):
            session.handle(self.request())
        self.assertEqual(len(list_pending(self.store)), 10)

    # Break caught: an ambiguous durable write failure allowing replay and a second record.
    def test_persistence_ambiguity_consumes_capability_and_cannot_create_twice(self) -> None:
        session = self.session()
        real_create = create_pending

        def persist_then_fail(*args: object, **kwargs: object):
            real_create(*args, **kwargs)  # type: ignore[arg-type]
            raise OSError("private-persistence-marker")

        with patch(
            "agent_container.family_intake_broker.create_pending",
            side_effect=persist_then_fail,
        ):
            with self.assertRaises(FamilyIntakeInternalError) as raised:
                session.handle(self.request())

        self.assertEqual(str(raised.exception), "family intake persistence failed")
        self.assertNotIn("private-persistence-marker", str(raised.exception))
        self.assertTrue(session.failed)
        self.assertTrue(session.consumed)
        self.assertEqual(len(list_pending(self.store)), 1)
        with self.assertRaises(FamilyIntakeDenied):
            session.handle(self.request())
        self.assertEqual(len(list_pending(self.store)), 1)

    # Break caught: an audit fsync ambiguity returning a replayable capability.
    def test_audit_failure_after_pending_creation_is_fail_closed(self) -> None:
        session = self.session()

        with patch(
            "agent_container.family_intake_broker.append_family_audit",
            side_effect=OSError("private-audit-marker"),
        ):
            with self.assertRaises(FamilyIntakeInternalError) as raised:
                session.handle(self.request())

        self.assertEqual(str(raised.exception), "family intake persistence failed")
        self.assertNotIn("private-audit-marker", str(raised.exception))
        self.assertTrue(session.failed)
        self.assertTrue(session.consumed)
        self.assertEqual(len(list_pending(self.store)), 1)
        with self.assertRaises(FamilyIntakeDenied):
            session.handle(self.request())
        self.assertEqual(len(list_pending(self.store)), 1)

    # Break caught: an ordinary request denial poisoning runtime supervision.
    def test_ordinary_denial_is_typed_without_marking_internal_failure(self) -> None:
        session = self.session()

        with self.assertRaises(FamilyIntakeDenied):
            session.handle(self.request(capability="x" * 43))

        self.assertFalse(session.failed)
        self.assertFalse(session.consumed)
        response = session.handle(self.request())
        self.assertEqual(response.status, "pending")
        self.assertEqual(len(list_pending(self.store)), 1)
        with self.assertRaises(FamilyIntakeDenied):
            session.handle(self.request())
        self.assertEqual(len(list_pending(self.store)), 1)

    # Break caught: wrong runtime identity reaching request parsing or persistence.
    def test_peer_pid_and_uid_must_match_expected_runtime(self) -> None:
        session = self.session()
        session.validate_peer(PEER_PID, os.getuid())

        for pid, uid in (
            (PEER_PID + 1, os.getuid()),
            (PEER_PID, os.getuid() + 1),
            (True, os.getuid()),
        ):
            with self.subTest(pid=pid, uid=uid):
                with self.assertRaises(ValueError):
                    session.validate_peer(pid, uid)
        self.assertFalse(session.consumed)

    # Break caught: invalid project/session primitives creating an unbound store record.
    def test_constructor_rejects_invalid_session_primitives_and_paths(self) -> None:
        cases = (
            {"project_id": "../other"},
            {"capability": "short"},
            {"expires_at": True},
            {"owner_uid": -1},
            {"process_reader": None},
            {"store": self.store.parent / "other" / "pending"},
            {"binding_path": self.store.parent / "other-binding.json"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises((TypeError, ValueError)):
                    self.session(**changes)

    # Break caught: a valid startup binding being replaced before capability use.
    def test_revalidates_binding_immediately_before_persistence(self) -> None:
        session = self.session()
        self.binding.write_text("{}\n", encoding="ascii")
        self.binding.chmod(0o600)

        with self.assertRaises(ValueError):
            session.handle(self.request())

        self.assertFalse(session.consumed)
        self.assertEqual(list_pending(self.store), ())


if __name__ == "__main__":
    unittest.main()
