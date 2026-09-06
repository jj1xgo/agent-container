"""Opt-in rootless Podman evidence that the agent's bwrap sandbox can start."""

import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import unittest

from agent_container.podman import run_codex_spec
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
class AgentSandboxPodmanIntegrationTest(unittest.TestCase):
    def test_codex_workspace_write_sandbox_runs_a_command(self) -> None:
        token = f"AGENT_SANDBOX_{secrets.token_hex(8)}"
        with tempfile.TemporaryDirectory(prefix="agent-container-sandbox-") as temporary:
            layout, handover_project = self._runtime_state(Path(temporary))
            spec = run_codex_spec(
                layout, handover_project, BASE_IMAGE, os.getuid(), os.getgid()
            )
            argv = [
                argument
                for argument in spec.argv
                if argument not in {"--interactive", "--tty"}
            ]
            agent_start = argv.index("codex", argv.index(BASE_IMAGE))
            command = (
                *argv[:agent_start],
                "codex",
                "--sandbox",
                "workspace-write",
                "sandbox",
                "--",
                "/usr/bin/printf",
                token,
            )

            completed = subprocess.run(
                command, check=False, capture_output=True, text=True, timeout=180
            )

        self.assertEqual(
            completed.returncode,
            0,
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
        )
        self.assertEqual(completed.stdout.strip(), token)
        self.assertNotIn("Can't mount proc", completed.stderr)

    @staticmethod
    def _runtime_state(root: Path) -> tuple[StateLayout, Path]:
        state = root / "state"
        handovers = root / "handovers"
        for directory in (
            state,
            state / "shared-auth/codex",
            state / "gh",
            state / "projects/agent-container/codex-home",
            state / "projects/agent-container/cache",
            state / "workspaces/agent-container/.git",
            handovers / "agent-container",
        ):
            directory.mkdir(mode=0o700, parents=True)
        auth = state / "shared-auth/codex/auth.json"
        auth.write_text("{}\n", encoding="utf-8")
        auth.chmod(0o600)
        layout = StateLayout(state.resolve(), "agent-container")
        return layout, (handovers / "agent-container").resolve()


if __name__ == "__main__":
    unittest.main()
