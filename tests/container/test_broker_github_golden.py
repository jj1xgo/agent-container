import json
from pathlib import Path
import unittest

from agent_container.github_broker_protocol import decode_request_frame
from agent_container.github_broker_protocol import decode_response_frame
from tests.container.broker_github_golden_support import collect_golden


class GitHubGoldenTest(unittest.TestCase):
    def test_wire_and_audit_match_original_commit(self):
        path = Path(__file__).resolve().parents[1] / "fixtures/broker_github_golden.json"
        expected = json.loads(path.read_text())
        self.assertEqual(collect_golden(), expected)
        for operation, encoded in expected["requests"].items():
            raw = bytes.fromhex(encoded)
            request, consumed = decode_request_frame(raw + b"tail")
            self.assertEqual((request.operation, consumed), (operation, len(raw)))
        for status, encoded in expected["responses"].items():
            raw = bytes.fromhex(encoded)
            response, consumed = decode_response_frame(raw + b"tail")
            self.assertEqual((response.status, consumed), (status, len(raw)))
