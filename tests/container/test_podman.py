from pathlib import Path
import os
import unittest

from agent_container.podman import auth_codex_spec
from agent_container.podman import build_image_spec
from agent_container.podman import clone_project_spec
from agent_container.podman import run_codex_spec
from agent_container.state import Repository
from agent_container.state import StateLayout


IMAGE = "localhost/agent-container:dev"


class PodmanCommandTest(unittest.TestCase):
    def test_build_uses_only_repository_context(self) -> None:
        spec = build_image_spec(Path("/repo"), IMAGE)
        self.assertEqual(
            spec.argv,
            ("podman", "build", "--tag", IMAGE, "--file", "/repo/Containerfile", "/repo"),
        )

    def test_auth_mounts_only_shared_codex_auth_directory(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        spec = auth_codex_spec(layout, IMAGE)
        joined = " ".join(spec.argv)
        self.assertIn("src=/state/shared-auth/codex,dst=/home/agent/.codex", joined)
        self.assertIn("codex login --device-auth", joined)
        self.assertNotIn("/workspace", joined)

    def test_clone_uses_read_only_gh_config_without_credential_content(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        repository = Repository.parse("jj1xgo/agent-container")
        spec = clone_project_spec(layout, repository, IMAGE)
        joined = " ".join(spec.argv)
        self.assertIn("src=/state/gh,dst=/home/agent/.config/gh,ro=true", joined)
        self.assertIn("jj1xgo/agent-container", joined)
        self.assertIn("/workspaces/agent-container", spec.argv)
        self.assertNotIn("credential-value", joined)
        self.assertNotIn("token", joined.lower())

    def test_run_has_hardened_flags_and_narrow_mounts(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        spec = run_codex_spec(
            layout=layout,
            handover_project=Path("/vault/handovers/agent-container"),
            image=IMAGE,
            uid=os.getuid(),
            gid=os.getgid(),
        )
        joined = " ".join(spec.argv)
        for required in ("--rm", "--read-only", "--cap-drop=all", "no-new-privileges"):
            self.assertIn(required, spec.argv if required != "no-new-privileges" else joined)
        self.assertIn("src=/state/workspaces/agent-container,dst=/workspace", joined)
        self.assertIn("src=/vault/handovers/agent-container,dst=/handovers/agent-container", joined)
        self.assertNotIn("/vault,dst=", joined)
        self.assertNotIn("token", joined.lower())

    def test_run_rejects_uid_or_gid_other_than_current_process(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        handover_project = Path("/vault/handovers/agent-container")
        with self.assertRaisesRegex(ValueError, "current user"):
            run_codex_spec(
                layout,
                handover_project,
                IMAGE,
                os.getuid() + 1,
                os.getgid(),
            )
        with self.assertRaisesRegex(ValueError, "current user"):
            run_codex_spec(
                layout,
                handover_project,
                IMAGE,
                os.getuid(),
                os.getgid() + 1,
            )


if __name__ == "__main__":
    unittest.main()
