from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
import unittest

from agent_container.handover_broker_client import HandoverBrokerClient
from agent_container.handover_broker_runtime import HandoverBrokerRuntime
from agent_container.state import StateLayout


RUN_SOCKET_INTEGRATION = (
    os.environ.get("AGENT_CONTAINER_RUN_SOCKET_INTEGRATION") == "1"
)

TITLE_MARKER = "private-title-marker"
BODY_MARKER = "private-body-marker"
VALID_BODY = f"""## 作業の目的
{BODY_MARKER}
## 現在地
現在地
## 決定事項と理由
決定
## 変更したファイル・commit・PR
変更
## 検証結果
検証
## 未解決事項とリスク
リスク
## 次の一手
次
""".encode("utf-8")
HANDOVER_NAME = re.compile(
    r"^\d{4}-\d{2}-\d{2}_\d{6}_[0-9a-f]{8}\.md$"
)


@unittest.skipUnless(
    RUN_SOCKET_INTEGRATION,
    "set AGENT_CONTAINER_RUN_SOCKET_INTEGRATION=1 for Unix socket integration",
)
class HandoverBrokerSocketIntegrationTest(unittest.TestCase):
    def test_denial_then_create_crosses_real_socket_and_cleans_runtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hb-") as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir(mode=0o700)
            handovers = root / "handovers"
            handovers.mkdir(mode=0o700)
            project = handovers / "agent-container"
            project.mkdir(mode=0o700)
            other_project = handovers / "other-project"
            other_project.mkdir(mode=0o700)
            layout = StateLayout(state.resolve(), "agent-container")
            runtime = HandoverBrokerRuntime.create(layout, project.resolve())
            run_dir = runtime.session.run_dir
            valid_capability = runtime.session._capability
            invalid_capability = "w" * 43
            invalid_capability_path = root / "invalid-capability"
            invalid_capability_path.write_text(
                invalid_capability + "\n",
                encoding="ascii",
            )
            invalid_capability_path.chmod(0o600)

            with runtime as mount:
                denied = HandoverBrokerClient(
                    mount.socket_path,
                    invalid_capability_path.resolve(),
                    "agent-container",
                )
                with self.assertRaises(RuntimeError):
                    denied.create(TITLE_MARKER, VALID_BODY)

                valid = HandoverBrokerClient(
                    mount.socket_path,
                    mount.capability_path,
                    "agent-container",
                )
                container_path = valid.create(TITLE_MARKER, VALID_BODY)

                filename = Path(container_path).name
                self.assertRegex(filename, HANDOVER_NAME)
                self.assertEqual(
                    container_path,
                    f"/handovers/agent-container/{filename}",
                )
                created = project / filename
                document = created.read_text(encoding="utf-8")
                self.assertTrue(
                    document.startswith(
                        f"# Handover: {TITLE_MARKER}\n\n"
                        "- Project: agent-container\n"
                        "- Created: "
                    )
                )
                created_line = document.splitlines()[3]
                created_time = datetime.fromisoformat(
                    created_line.removeprefix("- Created: ")
                )
                utc_offset = created_time.utcoffset()
                self.assertIsNotNone(utc_offset)
                self.assertEqual(
                    utc_offset.total_seconds() if utc_offset is not None else None,
                    0,
                )
                self.assertIn("- Session: （未記録）\n", document)
                self.assertTrue(document.endswith(VALID_BODY.decode("utf-8")))
                self.assertEqual(sorted(project.glob("*.md")), [created])
                self.assertEqual(list(other_project.iterdir()), [])

            self.assertFalse(run_dir.exists())
            self.assertFalse(runtime.session.socket_path.exists())
            self.assertFalse(runtime.session.capability_path.exists())
            with self.assertRaises((OSError, ValueError)):
                HandoverBrokerClient(
                    run_dir / "broker.sock",
                    run_dir / "capability",
                    "agent-container",
                ).create(TITLE_MARKER, VALID_BODY)

            records = [
                json.loads(line)
                for line in runtime.session.audit_file.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(
                [(record["status"], record["stage"]) for record in records],
                [("denied", "authentication"), ("ok", "write")],
            )
            audit = runtime.session.audit_file.read_text(encoding="utf-8")
            for secret in (
                invalid_capability,
                valid_capability,
                TITLE_MARKER,
                BODY_MARKER,
            ):
                self.assertNotIn(secret, audit)


if __name__ == "__main__":
    unittest.main()
