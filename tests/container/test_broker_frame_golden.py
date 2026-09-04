"""Golden frames captured from the pre-kernel handover encoder.

Generated at commit b1198d1 with agent_container.handover_broker_protocol
before any broker kernel existed. Never regenerate these bytes from the
kernel's own output; a mismatch here means the wire format changed.
"""

from io import BytesIO
import unittest

from agent_container.handover_broker_protocol import HandoverRequest
from agent_container.handover_broker_protocol import HandoverResponse
from agent_container.handover_broker_protocol import decode_request_frame
from agent_container.handover_broker_protocol import decode_response_frame
from agent_container.handover_broker_protocol import encode_request_frame
from agent_container.handover_broker_protocol import encode_response_frame
from agent_container.handover_broker_protocol import read_request_frame
from agent_container.handover_broker_protocol import read_response_frame


GOLDEN_BODY = "## 作業の目的\n目的\n\n## 現在地\n現在地\n"
GOLDEN_REQUEST = HandoverRequest(
    1, "A" * 43, "agent-container", "create", "Golden タイトル", GOLDEN_BODY
)
GOLDEN_REQUEST_BYTES = (
    b'\x00\x00\x00\xdb{"version":1,"capability":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",'
    b'"project_id":"agent-container","operation":"create",'
    b'"title":"Golden \xe3\x82\xbf\xe3\x82\xa4\xe3\x83\x88\xe3\x83\xab",'
    b'"body":"## \xe4\xbd\x9c\xe6\xa5\xad\xe3\x81\xae\xe7\x9b\xae\xe7\x9a\x84\\n'
    b'\xe7\x9b\xae\xe7\x9a\x84\\n\\n## \xe7\x8f\xbe\xe5\x9c\xa8\xe5\x9c\xb0\\n'
    b'\xe7\x8f\xbe\xe5\x9c\xa8\xe5\x9c\xb0\\n"}'
)
GOLDEN_RESPONSE_OK = HandoverResponse(
    1, "ok", "/handovers/agent-container/2026-09-04_000000_deadbeef.md", ""
)
GOLDEN_RESPONSE_OK_BYTES = (
    b'\x00\x00\x00g{"version":1,"status":"ok",'
    b'"path":"/handovers/agent-container/2026-09-04_000000_deadbeef.md","code":""}'
)
GOLDEN_RESPONSE_DENIED = HandoverResponse(1, "denied", "", "authentication")
GOLDEN_RESPONSE_DENIED_BYTES = (
    b'\x00\x00\x00A{"version":1,"status":"denied","path":"","code":"authentication"}'
)


class HandoverGoldenFrameTest(unittest.TestCase):
    def test_request_encodes_to_the_pre_kernel_bytes(self) -> None:
        self.assertEqual(encode_request_frame(GOLDEN_REQUEST), GOLDEN_REQUEST_BYTES)

    def test_request_bytes_decode_to_the_same_value(self) -> None:
        self.assertEqual(
            decode_request_frame(GOLDEN_REQUEST_BYTES),
            (GOLDEN_REQUEST, len(GOLDEN_REQUEST_BYTES)),
        )
        self.assertEqual(read_request_frame(BytesIO(GOLDEN_REQUEST_BYTES)), GOLDEN_REQUEST)

    def test_responses_encode_to_the_pre_kernel_bytes(self) -> None:
        self.assertEqual(encode_response_frame(GOLDEN_RESPONSE_OK), GOLDEN_RESPONSE_OK_BYTES)
        self.assertEqual(
            encode_response_frame(GOLDEN_RESPONSE_DENIED), GOLDEN_RESPONSE_DENIED_BYTES
        )

    def test_response_bytes_decode_to_the_same_values(self) -> None:
        self.assertEqual(
            decode_response_frame(GOLDEN_RESPONSE_OK_BYTES),
            (GOLDEN_RESPONSE_OK, len(GOLDEN_RESPONSE_OK_BYTES)),
        )
        self.assertEqual(
            read_response_frame(BytesIO(GOLDEN_RESPONSE_DENIED_BYTES)),
            GOLDEN_RESPONSE_DENIED,
        )


if __name__ == "__main__":
    unittest.main()
