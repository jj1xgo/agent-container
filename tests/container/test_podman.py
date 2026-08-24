from pathlib import Path
import os
import unittest

from agent_container.podman import auth_codex_spec
from agent_container.podman import codex_login_status_spec
from agent_container.podman import build_image_spec
from agent_container.podman import claude_setup_token_spec
from agent_container.podman import claude_token_status_spec
from agent_container.podman import cli_version_spec
from agent_container.podman import clone_project_spec
from agent_container.podman import run_codex_spec
from agent_container.podman import run_claude_spec
from agent_container.state import Repository
from agent_container.state import StateLayout


IMAGE = "localhost/agent-container:dev"


class PodmanCommandTest(unittest.TestCase):
    def test_build_uses_versions_cachebuster_and_repository_context(self) -> None:
        spec = build_image_spec(Path("/repo"), IMAGE, "0.149.0", "1.2.3", "12345")
        self.assertEqual(
            spec.argv,
            (
                "podman",
                "build",
                "--build-arg",
                "CODEX_VERSION=0.149.0",
                "--build-arg",
                "CLAUDE_VERSION=1.2.3",
                "--build-arg",
                "AGENT_CLI_CACHEBUST=12345",
                "--tag",
                IMAGE,
                "--file",
                "/repo/Containerfile",
                "/repo",
            ),
        )

    def test_cli_version_probes_are_hardened_and_mount_free(self) -> None:
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent):
                spec = cli_version_spec(IMAGE, agent)

                self.assertEqual(spec.argv[-2:], (agent, "--version"))
                self.assertNotIn("--mount", spec.argv)
                for required in (
                    "--rm",
                    "--read-only",
                    "--cap-drop=all",
                    "--security-opt=no-new-privileges",
                    "--userns=keep-id:uid=1000,gid=1000",
                    "--tmpfs=/tmp:rw,nosuid,nodev,size=512m",
                ):
                    self.assertIn(required, spec.argv)

    def test_auth_mounts_only_shared_codex_auth_directory(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        spec = auth_codex_spec(layout, IMAGE)
        joined = " ".join(spec.argv)
        self.assertIn("src=/state/shared-auth/codex,dst=/home/agent/.codex", joined)
        self.assertIn("codex login --device-auth", joined)
        self.assertNotIn("/workspace", joined)

    def test_login_status_uses_the_same_sanitized_auth_container(self) -> None:
        layout = StateLayout(Path("/state"), "auth")

        spec = codex_login_status_spec(layout, IMAGE)

        joined = " ".join(spec.argv)
        self.assertIn("src=/state/shared-auth/codex,dst=/home/agent/.codex", joined)
        self.assertEqual(spec.argv[-3:], ("codex", "login", "status"))
        self.assertNotIn("/workspace", joined)
        self.assertNotIn("token", joined.lower())

    def test_claude_setup_token_uses_only_ephemeral_claude_config(self) -> None:
        setup = claude_setup_token_spec(IMAGE)

        self.assertEqual(setup.argv[-2:], ("claude", "setup-token"))
        self.assertIn(
            "--tmpfs=/home/agent/.claude:rw,nosuid,nodev,noexec,size=16m",
            setup.argv,
        )
        self.assertIn("CLAUDE_CONFIG_DIR=/home/agent/.claude", setup.argv)
        self.assertNotIn("--mount", setup.argv)

    def test_claude_token_status_mounts_only_the_staged_token_read_only(self) -> None:
        status = claude_token_status_spec(Path("/private/staged"), IMAGE)

        joined = " ".join(status.argv)
        self.assertIn(
            "src=/private/staged,dst=/run/secrets/claude-oauth-token,ro=true",
            joined,
        )
        self.assertIn(
            "--tmpfs=/home/agent/.claude:rw,nosuid,nodev,noexec,size=16m",
            status.argv,
        )
        self.assertIn("CLAUDE_CONFIG_DIR=/home/agent/.claude", joined)
        self.assertEqual(
            status.argv[-9:],
            (
                IMAGE,
                "python3",
                "-m",
                "agent_container.claude_launcher",
                "/run/secrets/claude-oauth-token",
                "--",
                "claude",
                "auth",
                "status",
            ),
        )
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

    def test_claude_relocates_gh_config_without_changing_codex(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        handover_project = Path("/vault/handovers/agent-container")

        claude = run_claude_spec(
            layout,
            handover_project,
            IMAGE,
            os.getuid(),
            os.getgid(),
        )
        codex = run_codex_spec(
            layout,
            handover_project,
            IMAGE,
            os.getuid(),
            os.getgid(),
        )

        claude_joined = " ".join(claude.argv)
        self.assertIn("GH_CONFIG_DIR=/home/agent/gh-config", claude.argv)
        self.assertIn(
            "type=bind,src=/state/gh,dst=/home/agent/gh-config,ro=true",
            claude.argv,
        )
        self.assertNotIn("/home/agent/.config/gh", claude_joined)
        self.assertIn("GH_CONFIG_DIR=/home/agent/.config/gh", codex.argv)
        self.assertIn(
            "type=bind,src=/state/gh,dst=/home/agent/.config/gh,ro=true",
            codex.argv,
        )

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

    def test_claude_run_has_hardened_flags_and_isolated_mounts(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")

        spec = run_claude_spec(
            layout=layout,
            handover_project=Path("/vault/handovers/agent-container"),
            image=IMAGE,
            uid=os.getuid(),
            gid=os.getgid(),
        )

        joined = " ".join(spec.argv)
        for required in ("--rm", "--read-only", "--cap-drop=all", "no-new-privileges"):
            self.assertIn(required, spec.argv if required != "no-new-privileges" else joined)
        for source, target in (
            ("/state/workspaces/agent-container", "/workspace"),
            ("/state/projects/agent-container/claude-config", "/home/agent/.claude"),
            (
                "/state/shared-auth/claude/oauth-token",
                "/run/secrets/claude-oauth-token",
            ),
            ("/state/projects/agent-container/cache", "/home/agent/.cache"),
            ("/state/gh", "/home/agent/gh-config"),
            ("/vault/handovers/agent-container", "/handovers/agent-container"),
        ):
            self.assertIn(f"src={source},dst={target}", joined)
        self.assertIn(
            "type=bind,src=/state/projects/agent-container/claude-config,dst=/home/agent/.claude",
            spec.argv,
        )
        self.assertIn("src=/state/gh,dst=/home/agent/gh-config,ro=true", joined)
        self.assertIn(
            "src=/state/shared-auth/claude/oauth-token,dst=/run/secrets/claude-oauth-token,ro=true",
            joined,
        )
        self.assertIn("CLAUDE_CONFIG_DIR=/home/agent/.claude", joined)
        self.assertIn("AGENT_PROJECT_ID=agent-container", joined)
        self.assertIn("AGENT_HANDOVER_ROOT=/handovers", joined)
        self.assertEqual(
            spec.argv[-7:],
            (
                IMAGE,
                "python3",
                "-m",
                "agent_container.claude_launcher",
                "/run/secrets/claude-oauth-token",
                "--",
                "claude",
            ),
        )
        self.assertNotIn(".credentials.json", joined)
        self.assertNotIn("s" * 32, joined)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN=", joined)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", spec.environment)
        self.assertNotIn("dangerously-skip-permissions", joined)

    def test_claude_run_layers_private_home_tmpfs_before_nested_mounts(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")

        spec = run_claude_spec(
            layout=layout,
            handover_project=Path("/vault/handovers/agent-container"),
            image=IMAGE,
            uid=os.getuid(),
            gid=os.getgid(),
        )

        home_tmpfs = (
            "type=tmpfs,dst=/home/agent,tmpfs-size=16777216,"
            "tmpfs-mode=0700,U=true,noexec,nosuid,nodev"
        )
        self.assertIn(home_tmpfs, spec.argv)
        home_index = spec.argv.index(home_tmpfs)
        self.assertEqual(spec.argv[home_index - 1], "--mount")
        self.assertFalse(
            any(argument.startswith("--tmpfs=/home/agent:") for argument in spec.argv)
        )
        nested_mounts = (
            "type=bind,src=/state/projects/agent-container/claude-config,"
            "dst=/home/agent/.claude",
            "type=bind,src=/state/projects/agent-container/cache,"
            "dst=/home/agent/.cache",
            "type=bind,src=/state/gh,dst=/home/agent/gh-config,ro=true",
        )
        self.assertTrue(
            all(home_index < spec.argv.index(mount) for mount in nested_mounts)
        )

    def test_claude_run_rejects_uid_or_gid_other_than_current_process(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        handover_project = Path("/vault/handovers/agent-container")
        with self.assertRaisesRegex(ValueError, "current user"):
            run_claude_spec(
                layout,
                handover_project,
                IMAGE,
                os.getuid() + 1,
                os.getgid(),
            )
        with self.assertRaisesRegex(ValueError, "current user"):
            run_claude_spec(
                layout,
                handover_project,
                IMAGE,
                os.getuid(),
                os.getgid() + 1,
            )


if __name__ == "__main__":
    unittest.main()
