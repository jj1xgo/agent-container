"""Golden bytes captured from the pre-kernel egress broker.

Generated at commit 0ca61c2 with agent_container.egress_broker_protocol and
EgressBrokerSession.audit before the egress broker used the broker kernel.
Never regenerate these bytes from the kernel's own output; a mismatch here
means the wire format or the audit line format changed.
"""

from io import BytesIO
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from agent_container.egress_broker import EgressBrokerSession
from agent_container.egress_broker_protocol import EgressRequest
from agent_container.egress_broker_protocol import EgressResponse
from agent_container.egress_broker_protocol import decode_request_frame
from agent_container.egress_broker_protocol import decode_response_frame
from agent_container.egress_broker_protocol import encode_request_frame
from agent_container.egress_broker_protocol import encode_response_frame
from agent_container.egress_broker_protocol import read_request_frame
from agent_container.egress_broker_protocol import read_response_frame
from agent_container.egress_policy import EgressPolicy
from agent_container.state import StateLayout


GOLDEN_REQUEST = EgressRequest(
    1, "A" * 43, "agent-container", 1, "connect", "api.example.com", 443
)
GOLDEN_REQUEST_BYTES = (
    b'\x00\x00\x00\xb0{"capability":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",'
    b'"domain":"api.example.com","operation":"connect","port":443,'
    b'"project_id":"agent-container","sequence":1,"version":1}'
)
GOLDEN_RESPONSE_OK = EgressResponse(1, "ok", "connect")
GOLDEN_RESPONSE_OK_BYTES = b'\x00\x00\x00,{"code":"connect","status":"ok","version":1}'
GOLDEN_RESPONSE_DENIED = EgressResponse(1, "denied", "authentication")
GOLDEN_RESPONSE_DENIED_BYTES = (
    b'\x00\x00\x007{"code":"authentication","status":"denied","version":1}'
)
GOLDEN_RESPONSE_ERROR = EgressResponse(1, "error", "connect")
GOLDEN_RESPONSE_ERROR_BYTES = (
    b'\x00\x00\x00/{"code":"connect","status":"error","version":1}'
)

GOLDEN_RUN_LABEL = "0123456789abcdef"
GOLDEN_AUDIT_BYTES = (
    b'{"timestamp":"2026-09-04T00:00:00.000001+00:00","run":"0123456789abcdef",'
    b'"project":"agent-container","agent":"codex","operation":"connect",'
    b'"status":"ok","bytes_from_client":12,"bytes_from_upstream":34}\n'
    b'{"timestamp":"2026-09-04T00:00:00.000002+00:00","run":"0123456789abcdef",'
    b'"project":"agent-container","agent":"codex","operation":"connect",'
    b'"status":"denied","stage":"policy"}\n'
    b'{"timestamp":"2026-09-04T00:00:00.000003+00:00","run":"0123456789abcdef",'
    b'"project":"agent-container","agent":"codex","operation":"connect",'
    b'"status":"error","stage":"relay"}\n'
)


class EgressGoldenFrameTest(unittest.TestCase):
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
        self.assertEqual(
            encode_response_frame(GOLDEN_RESPONSE_ERROR), GOLDEN_RESPONSE_ERROR_BYTES
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
        self.assertEqual(
            decode_response_frame(GOLDEN_RESPONSE_ERROR_BYTES),
            (GOLDEN_RESPONSE_ERROR, len(GOLDEN_RESPONSE_ERROR_BYTES)),
        )


class EgressAuditGoldenTest(unittest.TestCase):
    def test_audit_lines_match_the_pre_kernel_writer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="egress-golden-") as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o700)
            layout = StateLayout(state.resolve(), "agent-container")
            policy = EgressPolicy(1, "allowlist", ("api.example.com",))
            session = EgressBrokerSession.create(layout, "codex", policy)
            try:
                stamps = iter(
                    [
                        "2026-09-04T00:00:00.000001+00:00",
                        "2026-09-04T00:00:00.000002+00:00",
                        "2026-09-04T00:00:00.000003+00:00",
                    ]
                )
                fixed_now = mock.Mock()
                fixed_now.isoformat.side_effect = lambda: next(stamps)
                with mock.patch(
                    "agent_container.egress_broker.datetime"
                ) as fixed_datetime, mock.patch.object(
                    EgressBrokerSession,
                    "run_label",
                    new_callable=mock.PropertyMock,
                    return_value=GOLDEN_RUN_LABEL,
                ):
                    fixed_datetime.now.return_value = fixed_now
                    session.audit("ok", bytes_from_client=12, bytes_from_upstream=34)
                    session.audit("denied", stage="policy")
                    session.audit("error", stage="relay")

                self.assertEqual(session.audit_file.read_bytes(), GOLDEN_AUDIT_BYTES)
                self.assertEqual(
                    stat.S_IMODE(session.audit_file.stat().st_mode), 0o600
                )
                self.assertEqual(
                    stat.S_IMODE(session.capability_path.stat().st_mode), 0o400
                )
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
