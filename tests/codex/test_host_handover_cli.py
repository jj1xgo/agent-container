from io import StringIO
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

    @patch("agent_container.host_handover_cli.publish_host_handover")
    @patch("agent_container.host_handover_cli._home_directory")
    def test_falls_back_to_claude_session_environment(
        self, home_directory, publish
    ) -> None:
        home_directory.return_value = Path("/trusted/home")
        publish.return_value = Path("/trusted/handover.md")

        status = main(
            ["publish", "--title", "Title", "--body-file", "/tmp/agent-handover-x.md"],
            environment={"CLAUDE_SESSION_ID": "claude-456"},
            cwd=Path("/trusted/workspace"),
        )

        self.assertEqual(status, 0)
        self.assertEqual(publish.call_args.kwargs["session_id"], "claude-456")

    @patch("agent_container.host_handover_cli.publish_host_handover")
    @patch("agent_container.host_handover_cli._home_directory")
    def test_prefers_codex_session_over_claude_session(
        self, home_directory, publish
    ) -> None:
        home_directory.return_value = Path("/trusted/home")
        publish.return_value = Path("/trusted/handover.md")

        main(
            ["publish", "--title", "Title", "--body-file", "/tmp/agent-handover-x.md"],
            environment={
                "CODEX_SESSION_ID": "codex-123",
                "CLAUDE_SESSION_ID": "claude-456",
            },
            cwd=Path("/trusted/workspace"),
        )

        self.assertEqual(publish.call_args.kwargs["session_id"], "codex-123")

    @patch("agent_container.host_handover_cli.discover_host_handover")
    @patch("agent_container.host_handover_cli._home_directory")
    def test_discover_prints_only_the_latest_handover_path(
        self, home_directory, discover
    ) -> None:
        home_directory.return_value = Path("/trusted/home")
        discover.return_value = Path("/trusted/handovers/p/2026-09-03_000000_ab.md")
        output = StringIO()

        with patch("sys.stdout", output):
            status = main(
                ["discover"],
                environment={"HOME": "/attacker/home"},
                cwd=Path("/trusted/workspace"),
            )

        self.assertEqual(status, 0)
        self.assertEqual(
            output.getvalue(), "/trusted/handovers/p/2026-09-03_000000_ab.md\n"
        )
        discover.assert_called_once_with(
            cwd=Path("/trusted/workspace"),
            projects_root=Path("/trusted/home/.local/share/agent-container/projects"),
        )

    @patch("agent_container.host_handover_cli.discover_host_handover")
    @patch("agent_container.host_handover_cli._home_directory")
    def test_discover_prints_nothing_without_handover(
        self, home_directory, discover
    ) -> None:
        home_directory.return_value = Path("/trusted/home")
        discover.return_value = None
        output = StringIO()

        with patch("sys.stdout", output):
            status = main(["discover"], environment={}, cwd=Path("/trusted/workspace"))

        self.assertEqual(status, 0)
        self.assertEqual(output.getvalue(), "")

    @patch("agent_container.host_handover_cli.discover_host_handover")
    @patch("agent_container.host_handover_cli._home_directory")
    def test_discover_refuses_quietly_when_workspace_is_unregistered(
        self, home_directory, discover
    ) -> None:
        home_directory.return_value = Path("/trusted/home")
        discover.side_effect = ValueError("workspace origin is unavailable")
        output = StringIO()
        errors = StringIO()

        with patch("sys.stdout", output), patch("sys.stderr", errors):
            status = main(["discover"], environment={}, cwd=Path("/trusted/workspace"))

        self.assertEqual(status, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "agent-handover-host: discovery refused\n")

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
