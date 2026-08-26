from pathlib import Path
import os
import unittest

from agent_container.podman import auth_codex_spec
from agent_container.podman import BrokerRuntimeMount
from agent_container.podman import codex_login_status_spec
from agent_container.podman import build_image_spec
from agent_container.podman import build_project_image_spec
from agent_container.podman import claude_setup_token_spec
from agent_container.podman import claude_policy_status_spec
from agent_container.podman import claude_superpowers_spec
from agent_container.podman import claude_superpowers_marketplace_spec
from agent_container.podman import claude_token_status_spec
from agent_container.podman import cli_version_spec
from agent_container.podman import clone_project_spec
from agent_container.podman import codex_superpowers_install_spec
from agent_container.podman import codex_superpowers_marketplace_spec
from agent_container.podman import podman_architecture_spec
from agent_container.podman import podman_image_id_spec
from agent_container.podman import podman_project_images_spec
from agent_container.podman import run_codex_spec
from agent_container.podman import run_claude_spec
from agent_container.state import Repository
from agent_container.state import StateLayout


IMAGE = "localhost/agent-container:dev"
DERIVED = "localhost/agent-container-project:sotlas-frontend-0123456789abcdef"


class PodmanCommandTest(unittest.TestCase):
    def test_superpowers_commands_use_agent_specific_project_state(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")

        marketplace = " ".join(
            codex_superpowers_marketplace_spec(layout, IMAGE).argv
        )
        self.assertNotIn("--interactive", marketplace)
        self.assertNotIn("--tty", marketplace)
        self.assertIn(
            "src=/state/projects/agent-container/codex-home,dst=/home/agent/.codex",
            marketplace,
        )
        self.assertIn(
            "codex plugin marketplace add obra/superpowers --ref main --json",
            marketplace,
        )
        install = " ".join(codex_superpowers_install_spec(layout, IMAGE).argv)
        self.assertIn("superpowers@superpowers-dev", install)

        update = " ".join(
            codex_superpowers_marketplace_spec(layout, IMAGE, update=True).argv
        )
        self.assertIn("marketplace upgrade superpowers-dev --json", update)

        claude_marketplace = " ".join(
            claude_superpowers_marketplace_spec(layout, IMAGE).argv
        )
        self.assertIn(
            "claude plugin marketplace add anthropics/claude-plugins-official",
            claude_marketplace,
        )
        claude_marketplace_update = " ".join(
            claude_superpowers_marketplace_spec(layout, IMAGE, update=True).argv
        )
        self.assertIn(
            "claude plugin marketplace update claude-plugins-official",
            claude_marketplace_update,
        )

        claude = " ".join(claude_superpowers_spec(layout, IMAGE).argv)
        self.assertIn(
            "src=/state/projects/agent-container/claude-config,dst=/home/agent/.claude",
            claude,
        )
        self.assertIn(
            "claude plugin install superpowers@claude-plugins-official --scope user --yes",
            claude,
        )
        claude_update = " ".join(
            claude_superpowers_spec(layout, IMAGE, update=True).argv
        )
        self.assertIn("claude plugin update superpowers@claude-plugins-official", claude_update)

    def test_claude_policy_probe_is_hardened_and_mount_free(self) -> None:
        spec = claude_policy_status_spec(IMAGE)

        self.assertEqual(
            spec.argv[-3:],
            ("python3", "-m", "agent_container.claude_policy"),
        )
        self.assertNotIn("--mount", spec.argv)
        for required in (
            "--rm",
            "--read-only",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--userns=keep-id:uid=1000,gid=1000",
        ):
            self.assertIn(required, spec.argv)

    def test_project_image_inspection_and_build_commands(self) -> None:
        self.assertEqual(
            podman_image_id_spec(IMAGE).argv,
            ("podman", "image", "inspect", "--format", "{{.Id}}", IMAGE),
        )
        self.assertEqual(
            podman_architecture_spec().argv,
            ("podman", "info", "--format", "{{.Host.Arch}}"),
        )
        self.assertEqual(
            podman_project_images_spec("sotlas-frontend").argv,
            (
                "podman",
                "images",
                "--filter",
                "reference=localhost/agent-container-project:sotlas-frontend-*",
                "--format",
                "{{.Repository}}:{{.Tag}}",
            ),
        )

        spec = build_project_image_spec(
            Path("/ctx"), Path("/ctx/Containerfile"), IMAGE, DERIVED
        )
        self.assertEqual(
            spec.argv,
            (
                "podman",
                "build",
                "--pull=never",
                "--build-arg",
                f"BASE_IMAGE={IMAGE}",
                "--tag",
                DERIVED,
                "--file",
                "/ctx/Containerfile",
                "/ctx",
            ),
        )
        self.assertEqual(spec.environment, {})

    def test_build_uses_versions_cachebuster_and_repository_context(self) -> None:
        spec = build_image_spec(
            Path("/repo"), IMAGE, "22.23.1", "0.149.0", "1.2.3", "12345"
        )
        self.assertEqual(
            spec.argv,
            (
                "podman",
                "build",
                "--build-arg",
                "NODE_VERSION=22.23.1",
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

    def test_node_version_probe_is_hardened_and_mount_free(self) -> None:
        from agent_container.podman import node_version_spec

        spec = node_version_spec(IMAGE)

        self.assertEqual(
            spec.argv[-2:], ("/opt/agent-node/bin/node", "--version")
        )
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

    def test_project_node_version_probe_uses_fixed_project_path(self) -> None:
        from agent_container.podman import project_node_version_spec

        spec = project_node_version_spec(IMAGE)

        self.assertEqual(
            spec.argv[-2:], ("/opt/project-node/bin/node", "--version")
        )
        self.assertNotIn("--mount", spec.argv)

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

    def test_clone_can_use_only_project_scoped_broker_runtime(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        repository = Repository.parse("jj1xgo/agent-container")
        broker = BrokerRuntimeMount(Path("/state/runtime/one"), repository)

        spec = clone_project_spec(layout, repository, IMAGE, broker)
        joined = " ".join(spec.argv)

        self.assertIn(
            "src=/state/runtime/one,dst=/run/agent-broker,ro=true", joined
        )
        self.assertIn("AGENT_BROKER_SOCKET=/run/agent-broker/broker.sock", joined)
        self.assertIn(
            "AGENT_BROKER_CAPABILITY=/run/agent-broker/capability", joined
        )
        self.assertIn("AGENT_BROKER_REPOSITORY=jj1xgo/agent-container", joined)
        self.assertIn(
            "GIT_CONFIG_KEY_0=url.agent-broker://jj1xgo/agent-container.insteadOf",
            joined,
        )
        self.assertEqual(
            spec.argv[-4:],
            (
                "git",
                "clone",
                "https://github.com/jj1xgo/agent-container.git",
                "/workspaces/agent-container",
            ),
        )
        self.assertNotIn("/state/gh", joined)
        self.assertNotIn("gh auth git-credential", joined)

    def test_runtime_can_replace_gh_mount_with_broker_runtime(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        repository = Repository.parse("jj1xgo/agent-container")
        broker = BrokerRuntimeMount(Path("/state/runtime/one"), repository)

        spec = run_codex_spec(
            layout,
            Path("/vault/handovers/agent-container"),
            IMAGE,
            os.getuid(),
            os.getgid(),
            broker,
        )
        joined = " ".join(spec.argv)

        self.assertIn("src=/state/runtime/one,dst=/run/agent-broker,ro=true", joined)
        self.assertNotIn("src=/state/gh", joined)
        self.assertNotIn("GH_CONFIG_DIR", joined)
        self.assertNotIn("gh auth git-credential", joined)

        claude = run_claude_spec(
            layout,
            Path("/vault/handovers/agent-container"),
            IMAGE,
            os.getuid(),
            os.getgid(),
            broker,
        )
        claude_joined = " ".join(claude.argv)
        self.assertIn(
            "src=/state/runtime/one,dst=/run/agent-broker,ro=true",
            claude_joined,
        )
        self.assertNotIn("src=/state/gh", claude_joined)
        self.assertNotIn("GH_CONFIG_DIR", claude_joined)

    def test_broker_mount_rejects_relative_path_and_wrong_repository(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        repository = Repository.parse("jj1xgo/agent-container")
        with self.assertRaisesRegex(ValueError, "absolute"):
            clone_project_spec(
                layout,
                repository,
                IMAGE,
                BrokerRuntimeMount(Path("relative"), repository),
            )
        with self.assertRaisesRegex(ValueError, "does not match"):
            clone_project_spec(
                layout,
                repository,
                IMAGE,
                BrokerRuntimeMount(
                    Path("/state/runtime/one"), Repository.parse("jj1xgo/other")
                ),
            )

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
        self.assertEqual(
            spec.argv[-5:],
            (
                IMAGE,
                "codex",
                "--approve-for-me",
                "-c",
                'tui.status_line=["model-with-reasoning","context-remaining",'
                '"five-hour-limit","weekly-limit","git-branch","project-name"]',
            ),
        )

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
