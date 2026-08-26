import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "container/bin/agent-handover"


class AgentHandoverWrapperTest(unittest.TestCase):
    def test_create_uses_fixed_environment_scope_and_session(self) -> None:
        with TemporaryDirectory() as temp:
            handover_root = Path(temp) / "handovers"
            (handover_root / "project").mkdir(parents=True)
            environment = {
                **os.environ,
                "PYTHONPATH": str(ROOT / "src"),
                "AGENT_HANDOVER_ROOT": str(handover_root),
                "AGENT_PROJECT_ID": "project",
                "CODEX_SESSION_ID": "session-123",
            }

            completed = subprocess.run(
                (str(WRAPPER), "create", "--title", "Safe title"),
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            created = Path(completed.stdout.strip())
            self.assertEqual(created.parent, handover_root / "project")
            body = created.read_text(encoding="utf-8")
            self.assertIn("# Handover: Safe title", body)
            self.assertIn("Session: session-123", body)

    def test_rejects_scope_override_arguments(self) -> None:
        completed = subprocess.run(
            (
                str(WRAPPER),
                "create",
                "--title",
                "Safe title",
                "--root",
                "/tmp/override",
            ),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("usage:", completed.stderr)
