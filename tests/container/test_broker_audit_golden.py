"""Golden audit lines captured from the pre-kernel handover writer.

Generated at commit 4c555fd with HandoverBrokerSession.audit before the
broker kernel owned audit files. Never regenerate these bytes from the
kernel's own output; a mismatch here means the audit line format changed.
"""

from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from agent_container.handover_broker import HandoverBrokerSession


GOLDEN_RUN_LABEL = "0123456789abcdef"
GOLDEN_PATH = "/handovers/agent-container/2026-09-04_000000_deadbeef.md"
GOLDEN_AUDIT_BYTES = (
    b'{"timestamp":"2026-09-04T00:00:00.000001+00:00","run":"0123456789abcdef",'
    b'"project":"agent-container","operation":"create","status":"ok","stage":"write",'
    b'"path":"/handovers/agent-container/2026-09-04_000000_deadbeef.md"}\n'
    b'{"timestamp":"2026-09-04T00:00:00.000002+00:00","run":"0123456789abcdef",'
    b'"project":"agent-container","operation":"create","status":"denied",'
    b'"stage":"authentication"}\n'
)


class HandoverAuditGoldenTest(unittest.TestCase):
    def test_audit_lines_match_the_pre_kernel_writer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="audit-golden-") as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir(mode=0o700)
            handovers = root / "handovers"
            handovers.mkdir(mode=0o700)
            project = handovers / "agent-container"
            project.mkdir(mode=0o700)
            session = HandoverBrokerSession.create(
                state.resolve(), "agent-container", project.resolve()
            )
            try:
                stamps = iter(
                    [
                        "2026-09-04T00:00:00.000001+00:00",
                        "2026-09-04T00:00:00.000002+00:00",
                    ]
                )
                fixed_now = mock.Mock()
                fixed_now.isoformat.side_effect = lambda: next(stamps)
                with mock.patch(
                    "agent_container.handover_broker.datetime"
                ) as fixed_datetime, mock.patch.object(
                    HandoverBrokerSession,
                    "run_label",
                    new_callable=mock.PropertyMock,
                    return_value=GOLDEN_RUN_LABEL,
                ):
                    fixed_datetime.now.return_value = fixed_now
                    session.audit("ok", stage="write", path=GOLDEN_PATH)
                    session.audit("denied", stage="authentication")

                self.assertEqual(session.audit_file.read_bytes(), GOLDEN_AUDIT_BYTES)
                self.assertEqual(
                    stat.S_IMODE(session.audit_file.stat().st_mode), 0o600
                )
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
