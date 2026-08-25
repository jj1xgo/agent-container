import io
import unittest
from unittest.mock import patch

from agent_container import git_remote_helper_cli


class GitRemoteHelperCliTests(unittest.TestCase):
    def test_failure_is_generic_and_closes_client(self) -> None:
        client = unittest.mock.Mock()
        stderr = io.StringIO()
        environment = {
            "AGENT_BROKER_SOCKET": "/run/agent/broker.sock",
            "AGENT_BROKER_CAPABILITY": "/run/agent/capability",
            "AGENT_PROJECT_ID": "project-1",
            "AGENT_BROKER_REPOSITORY": "owner/repository",
        }

        with (
            patch.dict(git_remote_helper_cli.os.environ, environment, clear=True),
            patch.object(git_remote_helper_cli, "BrokerUploadPackClient", return_value=client),
            patch.object(
                git_remote_helper_cli,
                "run_remote_helper",
                side_effect=OSError("sensitive path"),
            ),
            patch.object(git_remote_helper_cli.sys, "stderr", stderr),
            patch.object(git_remote_helper_cli.sys, "argv", ["helper", "origin", "agent-broker::repo"]),
        ):
            self.assertEqual(git_remote_helper_cli.main(), 1)

        self.assertEqual(stderr.getvalue(), "fatal: agent Git broker failed\n")
        client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
