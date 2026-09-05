import json
from pathlib import Path
import unittest

from agent_container.family_intake_protocol import decode_request_frame
from agent_container.family_intake_protocol import decode_response_frame
from tests.container.broker_family_golden_support import collect_golden
from tests.container.broker_family_golden_support import example_request
from tests.container.broker_family_golden_support import example_response


class FamilyGoldenTest(unittest.TestCase):
    def test_wire_and_audit_match_pre_kernel_commit(self):
        path = Path(__file__).resolve().parents[1] / "fixtures/broker_family_golden.json"
        expected = json.loads(path.read_text())
        self.assertEqual(collect_golden(), expected)
        self.assertEqual(expected["boundary_request_bytes"], 16384)
        for kind, decoder, value in (
            ("request", decode_request_frame, example_request()),
            ("response", decode_response_frame, example_response()),
        ):
            with self.subTest(kind=kind):
                frame = bytes.fromhex(expected[kind])
                self.assertEqual(decoder(frame + b"tail"), (value, len(frame)))
