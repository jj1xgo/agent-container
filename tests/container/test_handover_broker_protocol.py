from io import BytesIO
import json
import struct
import unittest

from agent_container.handover_broker_protocol import HandoverRequest
from agent_container.handover_broker_protocol import HandoverResponse
from agent_container.handover_broker_protocol import MAX_DOCUMENT_BYTES
from agent_container.handover_broker_protocol import MAX_REQUEST_BYTES
from agent_container.handover_broker_protocol import decode_request_frame
from agent_container.handover_broker_protocol import decode_response_frame
from agent_container.handover_broker_protocol import encode_request_frame
from agent_container.handover_broker_protocol import encode_response_frame
from agent_container.handover_broker_protocol import read_request_frame
from agent_container.handover_broker_protocol import read_response_frame


VALID_BODY = (
    "## 作業の目的\ncontent\n\n"
    "## 現在地\ncontent\n\n"
    "## 決定事項と理由\ncontent\n\n"
    "## 変更したファイル・commit・PR\ncontent\n\n"
    "## 検証結果\ncontent\n\n"
    "## 未解決事項とリスク\ncontent\n\n"
    "## 次の一手\ncontent\n"
)


def frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


class HandoverBrokerProtocolTest(unittest.TestCase):
    def test_request_round_trip_preserves_only_fixed_schema(self) -> None:
        request = HandoverRequest(
            1, "A" * 43, "agent-container", "create", "Safe title", VALID_BODY
        )
        self.assertEqual(read_request_frame(BytesIO(encode_request_frame(request))), request)

    def test_response_round_trip(self) -> None:
        response = HandoverResponse(1, "ok", "/handovers/agent-container/file.md", "")
        self.assertEqual(
            read_response_frame(BytesIO(encode_response_frame(response))), response
        )

    def test_rejects_duplicate_unknown_and_wrong_typed_fields(self) -> None:
        for payload in (
            b'{"version":1,"version":1}',
            b'{"version":true,"capability":"x","project_id":"p",'
            b'"operation":"create","title":"t","body":"b","extra":1}',
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                decode_request_frame(frame(payload))

    def test_rejects_missing_fields_and_wrong_request_field_types(self) -> None:
        baseline = {
            "version": 1,
            "capability": "x",
            "project_id": "p",
            "operation": "create",
            "title": "t",
            "body": "b",
        }
        cases = (
            {"version": False},
            {"capability": 1},
            {"project_id": None},
            {"operation": []},
            {"title": {}},
            {"body": 3},
        )
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                decode_request_frame(frame(json.dumps(baseline | changes).encode()))
        missing = dict(baseline)
        del missing["body"]
        with self.assertRaises(ValueError):
            decode_request_frame(frame(json.dumps(missing).encode()))

    def test_rejects_non_create_operations(self) -> None:
        for operation in ("list", "read", "edit", "rename", "overwrite", "delete"):
            payload = {
                "version": 1,
                "capability": "x",
                "project_id": "p",
                "operation": operation,
                "title": "t",
                "body": "b",
            }
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                decode_request_frame(frame(json.dumps(payload).encode()))

    def test_rejects_zero_oversize_truncated_utf8_and_nul_frames(self) -> None:
        invalid = (
            b"\x00\x00\x00\x00",
            struct.pack(">I", MAX_REQUEST_BYTES + 1),
            b"\x00\x00\x00\x05{}",
            b"\x00\x00\x00\x01\xff",
            frame(b'{"version":1,"capability":"x\\u0000","project_id":"p",'
                  b'"operation":"create","title":"t","body":"b"}'),
            frame(b'{"version":1,"capability":"x\\ud800","project_id":"p",'
                  b'"operation":"create","title":"t","body":"b"}'),
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                read_request_frame(BytesIO(payload))

    def test_readers_read_only_declared_length(self) -> None:
        request = HandoverRequest(1, "x", "p", "create", "t", "b")
        stream = BytesIO(encode_request_frame(request) + b"following")
        self.assertEqual(read_request_frame(stream), request)
        self.assertEqual(stream.read(), b"following")

    def test_rejects_nonstandard_json_numbers(self) -> None:
        payload = (
            b'{"version":1,"capability":"x","project_id":"p",'
            b'"operation":"create","title":"t","body":NaN}'
        )
        with self.assertRaises(ValueError):
            decode_request_frame(frame(payload))

    def test_response_status_and_code_combinations_are_fixed(self) -> None:
        valid = (
            HandoverResponse(1, "ok", "/handovers/p/file.md", ""),
            *(
                HandoverResponse(1, status, "", code)
                for status in ("denied", "error")
                for code in (
                    "authentication",
                    "schema",
                    "size",
                    "content-policy",
                    "filesystem-boundary",
                    "write",
                    "unavailable",
                )
            ),
        )
        for response in valid:
            with self.subTest(response=response):
                self.assertEqual(
                    read_response_frame(BytesIO(encode_response_frame(response))), response
                )

        invalid = (
            HandoverResponse(1, "ok", "", ""),
            HandoverResponse(1, "ok", "relative/file.md", ""),
            HandoverResponse(1, "ok", "/handovers/p/file.md", "write"),
            HandoverResponse(1, "denied", "/handovers/p/file.md", "schema"),
            HandoverResponse(1, "error", "", "other"),
            HandoverResponse(1, "unknown", "", "schema"),
        )
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(ValueError):
                encode_response_frame(response)

    def test_rejects_invalid_response_schema(self) -> None:
        for payload in (
            b'{"version":true,"status":"ok","path":"/x","code":""}',
            b'{"version":1,"status":"ok","path":"/x","code":"", "extra":1}',
            b'{"version":1,"status":"ok","path":"/x","code":""}'
            b'{"version":1,"status":"ok","path":"/x","code":""}',
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                decode_response_frame(frame(payload))

    def test_document_limit_is_the_fixed_value(self) -> None:
        self.assertEqual(MAX_DOCUMENT_BYTES, 65_536)


if __name__ == "__main__":
    unittest.main()
