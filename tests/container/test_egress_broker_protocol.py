import json
from io import BytesIO
import struct
import unittest

from agent_container.egress_broker_protocol import decode_request_frame
from agent_container.egress_broker_protocol import decode_response_frame
from agent_container.egress_broker_protocol import EgressRequest
from agent_container.egress_broker_protocol import EgressResponse
from agent_container.egress_broker_protocol import encode_request_frame
from agent_container.egress_broker_protocol import encode_response_frame
from agent_container.egress_broker_protocol import MAX_METADATA_BYTES
from agent_container.egress_broker_protocol import read_request_frame
from agent_container.egress_broker_protocol import read_response_frame


def raw_frame(body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + body


class ShortReads(BytesIO):
    def read(self, size: int = -1) -> bytes:
        return super().read(min(size, 2))


class EgressBrokerProtocolTest(unittest.TestCase):
    def test_round_trips_canonical_request_and_leaves_following_bytes(self) -> None:
        request = EgressRequest(
            1, "capability", "demo", 1, "connect", "api.example.com", 443
        )
        frame = encode_request_frame(request)

        decoded, consumed = decode_request_frame(frame + b"following")

        self.assertEqual(decoded, request)
        self.assertEqual((frame + b"following")[consumed:], b"following")

    def test_rejects_invalid_request_frames_and_json(self) -> None:
        duplicate = (
            b'{"version":1,"version":1,"capability":"c","project_id":"demo",'
            b'"sequence":1,"operation":"connect","domain":"example.com",'
            b'"port":443}'
        )
        cases = (
            b"",
            b"\x00\x00\x00",
            struct.pack(">I", 0),
            struct.pack(">I", MAX_METADATA_BYTES + 1),
            struct.pack(">I", 5) + b"{}",
            raw_frame(b"\xff"),
            raw_frame(b"not-json"),
            raw_frame(b'{"value":NaN}'),
            raw_frame(duplicate),
        )
        for data in cases:
            with self.subTest(data=data[:30]), self.assertRaises(ValueError):
                decode_request_frame(data)

    def test_rejects_wrong_request_schema_values_and_types(self) -> None:
        baseline = {
            "version": 1,
            "capability": "c",
            "project_id": "demo",
            "sequence": 1,
            "operation": "connect",
            "domain": "example.com",
            "port": 443,
        }
        changes = (
            {"extra": True},
            {"version": True},
            {"version": 2},
            {"capability": 1},
            {"project_id": []},
            {"sequence": True},
            {"sequence": 0},
            {"sequence": 1 << 63},
            {"operation": "resolve"},
            {"domain": "EXAMPLE.com"},
            {"domain": "127.0.0.1"},
            {"port": True},
            {"port": 80},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                decode_request_frame(raw_frame(json.dumps(baseline | change).encode()))

        missing = dict(baseline)
        del missing["domain"]
        with self.assertRaises(ValueError):
            decode_request_frame(raw_frame(json.dumps(missing).encode()))

    def test_response_round_trip_and_fixed_status_codes(self) -> None:
        for response in (
            EgressResponse(1, "ok", "connect"),
            EgressResponse(1, "denied", "policy"),
            EgressResponse(1, "error", "unavailable"),
        ):
            with self.subTest(response=response):
                frame = encode_response_frame(response)
                decoded, consumed = decode_response_frame(frame)
                self.assertEqual(decoded, response)
                self.assertEqual(consumed, len(frame))

        for response in (
            EgressResponse(2, "ok", "connect"),
            EgressResponse(1, "unknown", "connect"),
            EgressResponse(1, "denied", "secret-detail"),
        ):
            with self.subTest(response=response), self.assertRaises(ValueError):
                encode_response_frame(response)

        baseline = {"version": 1, "status": "denied", "code": "policy"}
        for change in (
            {"version": True},
            {"status": 1},
            {"code": False},
            {"extra": "value"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                decode_response_frame(
                    raw_frame(json.dumps(baseline | change).encode("ascii"))
                )

    def test_stream_readers_accept_short_reads_and_reject_truncation(self) -> None:
        request = EgressRequest(1, "c", "demo", 1, "connect", "example.com", 443)
        response = EgressResponse(1, "ok", "connect")
        self.assertEqual(
            read_request_frame(ShortReads(encode_request_frame(request))), request
        )
        self.assertEqual(
            read_response_frame(ShortReads(encode_response_frame(response))), response
        )
        with self.assertRaises(ValueError):
            read_request_frame(BytesIO(encode_request_frame(request)[:-1]))
        with self.assertRaises(ValueError):
            read_response_frame(BytesIO(encode_response_frame(response)[:-1]))


if __name__ == "__main__":
    unittest.main()
