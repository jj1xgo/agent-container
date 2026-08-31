from io import BytesIO
import json
import struct
import unittest

from agent_container.family_intake_protocol import FamilyIntakeRequest
from agent_container.family_intake_protocol import FamilyIntakeResponse
from agent_container.family_intake_protocol import MAX_REQUEST_BYTES
from agent_container.family_intake_protocol import MAX_RESPONSE_BYTES
from agent_container.family_intake_protocol import decode_request_frame
from agent_container.family_intake_protocol import decode_response_frame
from agent_container.family_intake_protocol import encode_request_frame
from agent_container.family_intake_protocol import encode_response_frame
from agent_container.family_intake_protocol import read_request_frame
from agent_container.family_intake_protocol import read_response_frame
from agent_container.family_intake_protocol import write_request_frame
from agent_container.family_intake_protocol import write_response_frame


def frame(body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + body


class SplitStream(BytesIO):
    def __init__(self, initial_bytes: bytes = b"", *, limit: int = 2) -> None:
        super().__init__(initial_bytes)
        self.limit = limit

    def read(self, size: int = -1) -> bytes:
        return super().read(min(size, self.limit))

    def write(self, body: bytes) -> int:
        return super().write(body[: self.limit])


class FamilyIntakeProtocolTest(unittest.TestCase):
    def test_request_round_trip_preserves_fixed_schema_and_trailing_bytes(self) -> None:
        request = FamilyIntakeRequest(
            version=1,
            operation="issue_create_request",
            capability="capability",
            payload={
                "title": "Add export",
                "summary": "Users need a portable copy.",
                "context": "The current UI has no export action.",
                "acceptance_criteria": ["A JSON file downloads"],
            },
        )

        encoded = encode_request_frame(request)
        decoded, consumed = decode_request_frame(encoded + b"following")

        self.assertEqual(decoded, request)
        self.assertEqual((encoded + b"following")[consumed:], b"following")
        self.assertLessEqual(len(encoded) - 4, MAX_REQUEST_BYTES)

    def test_response_round_trip_uses_pending_receipt_only(self) -> None:
        response = FamilyIntakeResponse(1, "pending", "request-123", 1_800_086_400)

        encoded = encode_response_frame(response)
        decoded, consumed = decode_response_frame(encoded)

        self.assertEqual(decoded, response)
        self.assertEqual(consumed, len(encoded))
        self.assertLessEqual(len(encoded) - 4, MAX_RESPONSE_BYTES)

    def test_readers_and_writers_handle_split_streams(self) -> None:
        request = FamilyIntakeRequest(
            1,
            "issue_create_request",
            "capability",
            {
                "title": "Title",
                "summary": "Summary",
                "context": "Context",
                "acceptance_criteria": ["Criterion"],
            },
        )
        response = FamilyIntakeResponse(1, "pending", "request-123", 1_800_086_400)
        request_stream, response_stream = SplitStream(), SplitStream()

        write_request_frame(request_stream, request)
        write_response_frame(response_stream, response)

        self.assertEqual(
            read_request_frame(SplitStream(request_stream.getvalue())), request
        )
        self.assertEqual(
            read_response_frame(SplitStream(response_stream.getvalue())), response
        )

    def test_rejects_eof_zero_oversized_and_invalid_json_frames(self) -> None:
        cases = (
            b"",
            b"\x00\x00\x00",
            struct.pack(">I", 0),
            struct.pack(">I", MAX_REQUEST_BYTES + 1),
            struct.pack(">I", 5) + b"{}",
            frame(b"\xff"),
            frame(b"not-json"),
            frame(b'{"value":NaN}'),
        )
        for data in cases:
            with self.subTest(data=data[:20]), self.assertRaises(ValueError):
                decode_request_frame(data)

        with self.assertRaises(ValueError):
            read_request_frame(BytesIO(encode_request_frame(self._request())[:-1]))
        with self.assertRaises(ValueError):
            read_response_frame(BytesIO(encode_response_frame(self._response())[:-1]))

    def test_rejects_duplicate_raw_json_keys_before_payload_materializes(self) -> None:
        payload = (
            b'{"version":1,"operation":"issue_create_request",'
            b'"capability":"c","payload":{"title":"one","title":"two",'
            b'"summary":"summary","context":"context",'
            b'"acceptance_criteria":["criterion"]}}'
        )

        with self.assertRaises(ValueError):
            decode_request_frame(frame(payload))

    def test_request_codec_requires_the_exact_validated_draft_payload(self) -> None:
        valid = self._request().payload
        invalid_payloads = (
            {},
            valid | {"repository": "private/repository"},
            valid | {"unknown": True},
            {key: value for key, value in valid.items() if key != "context"},
            valid | {"title": "invalid\ncontrol"},
            valid | {"summary": "x" * (2 * 1024 + 1)},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                request = FamilyIntakeRequest(
                    1, "issue_create_request", "capability", payload
                )
                with self.assertRaises(ValueError):
                    encode_request_frame(request)

                raw = json.dumps(
                    {
                        "version": 1,
                        "operation": "issue_create_request",
                        "capability": "capability",
                        "payload": payload,
                    }
                ).encode()
                with self.assertRaises(ValueError):
                    decode_request_frame(frame(raw))

    def test_rejects_unknown_wrong_version_operation_status_and_trailing_frames(self) -> None:
        request = {
            "version": 1,
            "operation": "issue_create_request",
            "capability": "c",
            "payload": {"title": "title"},
        }
        for changed in (
            {"unknown": True},
            {"version": True},
            {"version": 2},
            {"operation": "issue_delete"},
            {"capability": ""},
            {"payload": []},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                decode_request_frame(frame(json.dumps(request | changed).encode()))

        response = {
            "version": 1,
            "status": "pending",
            "request_id": "request-123",
            "expires_at": 1_800_086_400,
        }
        for changed in (
            {"version": True},
            {"version": 2},
            {"status": "created"},
            {"request_id": "bad\nrequest"},
            {"expires_at": True},
            {"expires_at": 0},
            {"extra": "value"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                decode_response_frame(frame(json.dumps(response | changed).encode()))

    def test_rejects_response_over_the_fixed_maximum(self) -> None:
        response = FamilyIntakeResponse(1, "pending", "r" * MAX_RESPONSE_BYTES, 1)

        with self.assertRaises(ValueError):
            encode_response_frame(response)

    @staticmethod
    def _request() -> FamilyIntakeRequest:
        return FamilyIntakeRequest(
            1,
            "issue_create_request",
            "capability",
            {
                "title": "Title",
                "summary": "Summary",
                "context": "Context",
                "acceptance_criteria": ["Criterion"],
            },
        )

    @staticmethod
    def _response() -> FamilyIntakeResponse:
        return FamilyIntakeResponse(1, "pending", "request-123", 1_800_086_400)
