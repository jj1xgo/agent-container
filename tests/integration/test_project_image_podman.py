from io import StringIO
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid

from agent_container.agentctl import _resolve_project_image
from agent_container.podman import CommandSpec
from agent_container.podman import run_command
from agent_container.state import StateLayout


RUN_PODMAN_INTEGRATION = (
    os.environ.get("AGENT_CONTAINER_RUN_PODMAN_INTEGRATION") == "1"
)
BASE_IMAGE = os.environ.get(
    "AGENT_CONTAINER_INTEGRATION_BASE_IMAGE",
    "localhost/agent-container:dev",
)


@unittest.skipUnless(
    RUN_PODMAN_INTEGRATION,
    "set AGENT_CONTAINER_RUN_PODMAN_INTEGRATION=1 for real Podman tests",
)
class ProjectImagePodmanIntegrationTest(unittest.TestCase):
    def test_builds_reuses_and_runs_derived_project_image(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-container-integration-") as temporary:
            root = Path(temporary) / "state"
            project_id = f"integration-{uuid.uuid4().hex[:12]}"
            layout = StateLayout(root, project_id)
            config = layout.workspace / ".agent-container.d"
            config.mkdir(parents=True)
            (config / "packages.txt").write_text("make\n", encoding="utf-8")
            (config / "node-version.txt").write_text(
                "22.23.1\n", encoding="utf-8"
            )
            calls: list[tuple[str, ...]] = []

            def runner(spec: CommandSpec) -> subprocess.CompletedProcess[str]:
                calls.append(spec.argv)
                is_build = spec.argv[:2] == ("podman", "build")
                return run_command(
                    spec,
                    check=False,
                    capture_output=not is_build,
                )

            first = _resolve_project_image(
                layout,
                BASE_IMAGE,
                runner,
                build_missing=True,
                stdout=StringIO(),
            )
            self.addCleanup(
                subprocess.run,
                ("podman", "image", "rm", "--force", first.image),
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            first_build_count = sum(
                argv[:2] == ("podman", "build") for argv in calls
            )

            second = _resolve_project_image(
                layout,
                BASE_IMAGE,
                runner,
                build_missing=True,
                stdout=StringIO(),
            )

            self.assertEqual(first.state, "current")
            self.assertEqual(second, first)
            self.assertEqual(first_build_count, 1)
            self.assertEqual(
                sum(argv[:2] == ("podman", "build") for argv in calls),
                first_build_count,
            )
            self.assertTrue(self._run(first.image, "make", "--version").startswith("GNU Make"))
            self.assertEqual(
                self._run(first.image, "/opt/project-node/bin/node", "--version"),
                "v22.23.1",
            )
            self.assertRegex(
                self._run(first.image, "/opt/agent-node/bin/node", "--version"),
                r"^v[0-9]+\.[0-9]+\.[0-9]+$",
            )
            self.assertTrue(self._run(first.image, "codex", "--version"))
            self.assertTrue(self._run(first.image, "claude", "--version"))

    def _run(self, image: str, *command: str) -> str:
        completed = subprocess.run(
            (
                "podman",
                "run",
                "--rm",
                "--read-only",
                "--cap-drop=all",
                "--security-opt=no-new-privileges",
                "--userns=keep-id:uid=1000,gid=1000",
                "--tmpfs=/tmp:rw,nosuid,nodev,size=512m",
                image,
                *command,
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()


if __name__ == "__main__":
    unittest.main()
