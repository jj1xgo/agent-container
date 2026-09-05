from dataclasses import replace
import os
from pathlib import Path
import tempfile
import threading
import unittest

from agent_container.egress_adapter import EgressSequence
from agent_container.egress_broker import EgressBrokerSession
from agent_container.egress_broker_protocol import EgressRequest, MAX_SEQUENCE
from agent_container.egress_policy import EgressPolicy
from agent_container.state import StateLayout


class EgressSequenceReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="es-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "s"
        root.mkdir(mode=0o700)
        self.session = EgressBrokerSession.create(
            StateLayout(root, "probe"), "codex",
            EgressPolicy(1, "allowlist", ("allowed.example.com",)),
        )
        self.addCleanup(self.session.close)
        self.sequence = EgressSequence()

    def request(self, sequence: int, domain: str = "allowed.example.com") -> EgressRequest:
        return EgressRequest(
            1, self.session._capability, "probe", sequence, "connect", domain, 443,
        )

    def authorize(self, request: EgressRequest) -> str:
        return self.session.authorize(request, os.getuid())

    def test_denial_does_not_poison_the_next_adapter_request(self) -> None:
        denied = self.request(self.sequence.next(), "denied.example.com")
        with self.assertRaises(ValueError) as raised:
            self.authorize(denied)
        self.assertEqual(getattr(raised.exception, "stage", None), "policy")
        allowed = self.request(self.sequence.next())
        self.assertEqual(self.authorize(allowed), "allowed.example.com")
        with self.assertRaisesRegex(ValueError, "sequence"):
            self.authorize(replace(denied, domain="allowed.example.com"))

    def test_reordered_adapter_requests_and_followup_are_accepted_once(self) -> None:
        first, second = (self.request(self.sequence.next()) for _ in range(2))
        for request in (second, first, self.request(self.sequence.next())):
            self.assertEqual(self.authorize(request), "allowed.example.com")
            with self.assertRaisesRegex(ValueError, "sequence"):
                self.authorize(request)

    def test_unauthenticated_requests_do_not_consume_or_advance_window(self) -> None:
        future = self.request(MAX_SEQUENCE)
        for request, peer in (
            (replace(future, capability="invalid"), os.getuid()),
            (replace(future, version=2), os.getuid()),
            (replace(future, project_id="other"), os.getuid()),
            (future, os.getuid() + 1),
        ):
            with self.subTest(peer_matches=peer == os.getuid()):
                with self.assertRaises(ValueError):
                    self.session.authorize(request, peer)
        self.assertEqual(self.authorize(self.request(1)), "allowed.example.com")
        self.assertEqual(self.authorize(future), "allowed.example.com")

    def test_window_boundary_rejects_old_requests_without_poisoning_new_ones(self) -> None:
        self.assertEqual(self.authorize(self.request(4096)), "allowed.example.com")
        self.assertEqual(self.authorize(self.request(1)), "allowed.example.com")
        self.assertEqual(self.authorize(self.request(4097)), "allowed.example.com")
        with self.assertRaisesRegex(ValueError, "sequence"):
            self.authorize(self.request(1))
        self.assertEqual(self.authorize(self.request(2)), "allowed.example.com")
        with self.assertRaisesRegex(ValueError, "sequence"):
            self.authorize(self.request(4096))

    def test_large_jump_is_bounded_and_recent_holes_are_usable(self) -> None:
        self.assertEqual(self.authorize(self.request(1)), "allowed.example.com")
        self.assertEqual(self.authorize(self.request(MAX_SEQUENCE)), "allowed.example.com")
        self.assertEqual(
            self.authorize(self.request(MAX_SEQUENCE - 4095)), "allowed.example.com",
        )
        for sequence in (1, MAX_SEQUENCE - 4096, MAX_SEQUENCE):
            with self.subTest(sequence=sequence), self.assertRaisesRegex(ValueError, "sequence"):
                self.authorize(self.request(sequence))

    def test_invalid_sequence_cannot_consume_a_valid_number(self) -> None:
        for sequence in (True, False, 0, -1, MAX_SEQUENCE + 1, 1.0):
            with self.subTest(sequence=sequence), self.assertRaises(ValueError):
                self.authorize(self.request(sequence))
        self.assertEqual(self.authorize(self.request(1)), "allowed.example.com")

    def test_authenticated_invalid_operation_and_port_are_consumed(self) -> None:
        for sequence, changes in ((1, {"operation": "resolve"}), (2, {"port": 80})):
            with self.assertRaises(ValueError):
                self.authorize(replace(self.request(sequence), **changes))
            with self.assertRaisesRegex(ValueError, "sequence"):
                self.authorize(self.request(sequence))
        self.assertEqual(self.authorize(self.request(3)), "allowed.example.com")

    def test_concurrent_duplicate_is_accepted_exactly_once(self) -> None:
        gate = threading.Barrier(3)
        outcomes = []
        request = self.request(2)

        def worker() -> None:
            gate.wait(timeout=5)
            try:
                self.authorize(request)
                outcomes.append("allowed")
            except ValueError:
                outcomes.append("denied")

        workers = [threading.Thread(target=worker) for _ in range(2)]
        for worker_thread in workers:
            worker_thread.start()
        gate.wait(timeout=5)
        for worker_thread in workers:
            worker_thread.join(timeout=5)
            self.assertFalse(worker_thread.is_alive())
        self.assertCountEqual(outcomes, ["allowed", "denied"])
        self.assertEqual(self.authorize(self.request(1)), "allowed.example.com")
