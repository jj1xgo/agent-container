from pathlib import Path
from unittest.mock import patch
import unittest

from agent_container.host_handover_cli import main


class HostHandoverCliTest(unittest.TestCase):
    @patch("agent_container.host_handover_cli.publish_host_handover")
    @patch("agent_container.host_handover_cli._home_directory")
    def test_uses_fixed_state_location_and_session_environment(
        self, home_directory, publish
    ) -> None:
        home_directory.return_value = Path("/trusted/home")
        publish.return_value = Path("/trusted/handover.md")

        status = main(
            ["publish", "--title", "Title", "--body-file", "/tmp/agent-handover-x.md"],
            environment={
                "CODEX_SESSION_ID": "session-123",
                "HOME": "/attacker/home",
                "AGENT_CONTAINER_HOME": "/attacker/state",
            },
            cwd=Path("/trusted/workspace"),
        )

        self.assertEqual(status, 0)
        publish.assert_called_once_with(
            cwd=Path("/trusted/workspace"),
            projects_root=Path("/trusted/home/.local/share/agent-container/projects"),
            title="Title",
            body_file=Path("/tmp/agent-handover-x.md"),
            session_id="session-123",
        )

    def test_destination_override_is_not_an_option(self) -> None:
        with self.assertRaises(SystemExit):
            main(
                [
                    "publish",
                    "--title",
                    "Title",
                    "--body-file",
                    "/tmp/agent-handover-x.md",
                    "--project",
                    "other",
                ],
                environment={},
                cwd=Path("/trusted/workspace"),
            )


if __name__ == "__main__":
    unittest.main()
