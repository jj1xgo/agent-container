from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import unittest

from agent_container.agentctl import main


class AgentCtlBuildAuthTest(unittest.TestCase):
    def test_build_runs_one_podman_build(self) -> None:
        calls = []
        result = main(
            ["build"],
            runner=lambda spec: calls.append(spec)
            or subprocess.CompletedProcess(spec.argv, 0),
        )
        self.assertEqual(result, 0)
        self.assertEqual(calls[0].argv[:2], ("podman", "build"))

    def test_auth_creates_private_state_and_runs_device_login(self) -> None:
        with TemporaryDirectory() as temp:
            calls = []
            environment = {"AGENT_CONTAINER_HOME": temp}

            def runner(spec):
                calls.append(spec)
                auth_file = Path(temp) / "shared-auth/codex/auth.json"
                auth_file.write_text("fixture-not-a-token", encoding="utf-8")
                auth_file.chmod(0o600)
                return subprocess.CompletedProcess(spec.argv, 0)

            result = main(["auth", "codex"], environment=environment, runner=runner)
            self.assertEqual(result, 0)
            self.assertEqual(
                (Path(temp) / "shared-auth/codex").stat().st_mode & 0o777, 0o700
            )
            self.assertIn("--device-auth", calls[0].argv)

    def test_auth_error_does_not_print_credential_content(self) -> None:
        with TemporaryDirectory() as temp:
            auth_dir = Path(temp) / "shared-auth/codex"
            auth_dir.mkdir(parents=True, mode=0o700)
            auth_dir.parent.chmod(0o700)
            auth_file = auth_dir / "auth.json"
            auth_file.write_text("DO-NOT-PRINT-CREDENTIAL-BODY", encoding="utf-8")
            auth_file.chmod(0o644)
            stderr = StringIO()

            result = main(
                ["auth", "codex"],
                environment={"AGENT_CONTAINER_HOME": temp},
                stderr=stderr,
            )

            self.assertEqual(result, 1)
            self.assertIn("mode 0600", stderr.getvalue())
            self.assertNotIn("DO-NOT-PRINT-CREDENTIAL-BODY", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
