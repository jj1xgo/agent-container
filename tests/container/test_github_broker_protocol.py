import json
import struct
import unittest

from agent_container.github_broker_protocol import BrokerRequest
from agent_container.github_broker_protocol import MAX_REQUEST_BYTES
from agent_container.github_broker_protocol import decode_request_frame
from agent_container.github_broker_protocol import encode_request_frame


def raw_frame(body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + body


class BrokerProtocolTest(unittest.TestCase):
    def test_round_trips_one_frame_and_leaves_following_bytes(self) -> None:
        request = BrokerRequest(
            version=1,
            capability="safe-capability",
            project_id="agent-container",
            sequence=1,
            operation="pr-view",
            payload={"number": 10},
        )
        frame = encode_request_frame(request)

        decoded, consumed = decode_request_frame(frame + b"following")

        self.assertEqual(decoded, request)
        self.assertEqual(frame[consumed:], b"")
        self.assertEqual((frame + b"following")[consumed:], b"following")

    def test_rejects_incomplete_invalid_and_oversized_frames(self) -> None:
        cases = (
            b"",
            b"\x00\x00\x00",
            struct.pack(">I", 0),
            struct.pack(">I", MAX_REQUEST_BYTES + 1),
            struct.pack(">I", 5) + b"{}",
            raw_frame(b"\xff"),
            raw_frame(b"not-json"),
        )
        for data in cases:
            with self.subTest(data=data[:20]):
                with self.assertRaises(ValueError):
                    decode_request_frame(data)

    def test_rejects_duplicate_unknown_or_missing_fields(self) -> None:
        duplicate = (
            b'{"version":1,"version":1,"capability":"c","project_id":"p",'
            b'"sequence":1,"operation":"pr-view","payload":{}}'
        )
        unknown = {
            "version": 1,
            "capability": "c",
            "project_id": "p",
            "sequence": 1,
            "operation": "pr-view",
            "payload": {},
            "extra": True,
        }
        missing = dict(unknown)
        del missing["extra"]
        del missing["payload"]
        for body in (
            duplicate,
            json.dumps(unknown).encode(),
            json.dumps(missing).encode(),
        ):
            with self.subTest(body=body[:40]):
                with self.assertRaises(ValueError):
                    decode_request_frame(raw_frame(body))

    def test_rejects_wrong_field_types_and_nonstandard_numbers(self) -> None:
        baseline = {
            "version": 1,
            "capability": "c",
            "project_id": "p",
            "sequence": 1,
            "operation": "pr-view",
            "payload": {},
        }
        cases = (
            {"version": True},
            {"capability": 1},
            {"project_id": []},
            {"sequence": True},
            {"operation": None},
            {"payload": []},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                body = json.dumps(baseline | changes).encode()
                with self.assertRaises(ValueError):
                    decode_request_frame(raw_frame(body))

        nan = json.dumps(baseline).replace('"sequence": 1', '"sequence": NaN')
        with self.assertRaises(ValueError):
            decode_request_frame(raw_frame(nan.encode()))

    def test_encoder_rejects_oversized_payload(self) -> None:
        request = BrokerRequest(1, "c", "p", 1, "pr-view", {"body": "x" * 70_000})
        with self.assertRaisesRegex(ValueError, "too large"):
            encode_request_frame(request)
