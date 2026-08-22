from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import unittest

from agent_container.agentctl import main
from agent_container.state import ProjectRecord


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


class AgentCtlProjectTest(unittest.TestCase):
    def _authenticated_state(self, root: Path) -> None:
        hosts = root / "gh/hosts.yml"
        hosts.parent.mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        hosts.write_text("github.com:\n", encoding="utf-8")
        hosts.chmod(0o600)

    def test_project_add_records_repository_after_clone(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            handovers = Path(temp) / "handovers"
            (handovers / "agent-container").mkdir(parents=True)
            self._authenticated_state(root)
            calls = []

            def runner(spec):
                calls.append(spec)
                workspace = root / "workspaces/agent-container"
                workspace.mkdir(parents=True)
                (workspace / ".git").mkdir()
                return subprocess.CompletedProcess(spec.argv, 0)

            result = main(
                ["project", "add", "jj1xgo/agent-container", "--handover-root", str(handovers)],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=runner,
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
            )

            self.assertEqual(result, 0)
            record = ProjectRecord.read(root / "projects/agent-container/project.json")
            self.assertEqual(record.repository.slug, "jj1xgo/agent-container")
            self.assertTrue((root / "projects/agent-container/codex-home/config.toml").is_file())
            self.assertEqual(len(calls), 1)
            for forbidden in ("checkout", "reset", "clean", "fetch"):
                self.assertNotIn(forbidden, calls[0].argv)

    def test_project_add_leaves_existing_workspace_directory_untouched(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            handovers = Path(temp) / "handovers"
            (handovers / "agent-container").mkdir(parents=True)
            self._authenticated_state(root)
            workspace = root / "workspaces/agent-container"
            workspace.mkdir(parents=True)
            workspace.parent.chmod(0o700)
            marker = workspace / "KEEP-ME"
            marker.write_text("ordinary-directory", encoding="utf-8")
            calls = []

            result = main(
                ["project", "add", "jj1xgo/agent-container", "--handover-root", str(handovers)],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=lambda spec: calls.append(spec) or subprocess.CompletedProcess(spec.argv, 0),
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
            )

            self.assertEqual(result, 1)
            self.assertEqual(marker.read_text(encoding="utf-8"), "ordinary-directory")
            self.assertEqual(calls, [])
            self.assertFalse((root / "projects/agent-container/project.json").exists())

    def test_project_add_rejects_mismatched_origin_without_metadata(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            handovers = Path(temp) / "handovers"
            (handovers / "agent-container").mkdir(parents=True)
            self._authenticated_state(root)

            def runner(spec):
                (root / "workspaces/agent-container/.git").mkdir(parents=True)
                return subprocess.CompletedProcess(spec.argv, 0)

            result = main(
                ["project", "add", "jj1xgo/agent-container", "--handover-root", str(handovers)],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=runner,
                git_remote_reader=lambda path: "https://github.com/example/other.git",
            )

            self.assertEqual(result, 1)
            self.assertFalse((root / "projects/agent-container/project.json").exists())

    def test_project_add_rejects_symlinked_handover_project_before_clone(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            handovers = Path(temp) / "handovers"
            target = Path(temp) / "target"
            target.mkdir()
            handovers.mkdir()
            (handovers / "agent-container").symlink_to(target, target_is_directory=True)
            calls = []

            result = main(
                ["project", "add", "jj1xgo/agent-container", "--handover-root", str(handovers)],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=lambda spec: calls.append(spec) or subprocess.CompletedProcess(spec.argv, 0),
            )

            self.assertEqual(result, 1)
            self.assertEqual(calls, [])

    def test_project_add_rejects_symlinked_handover_root_before_clone(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            handovers = Path(temp) / "handovers"
            handovers.mkdir()
            (handovers / "agent-container").mkdir()
            linked_root = Path(temp) / "linked-handovers"
            linked_root.symlink_to(handovers, target_is_directory=True)
            calls = []

            result = main(
                ["project", "add", "jj1xgo/agent-container", "--handover-root", str(linked_root)],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=lambda spec: calls.append(spec) or subprocess.CompletedProcess(spec.argv, 0),
            )

            self.assertEqual(result, 1)
            self.assertEqual(calls, [])

    def test_project_add_leaves_partial_clone_marker_after_failure(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            handovers = Path(temp) / "handovers"
            (handovers / "agent-container").mkdir(parents=True)
            self._authenticated_state(root)
            marker = root / "workspaces/agent-container/KEEP-ME"

            def runner(spec):
                marker.parent.mkdir(parents=True)
                marker.write_text("partial-clone", encoding="utf-8")
                raise subprocess.CalledProcessError(9, spec.argv)

            result = main(
                ["project", "add", "jj1xgo/agent-container", "--handover-root", str(handovers)],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=runner,
            )

            self.assertEqual(result, 9)
            self.assertEqual(marker.read_text(encoding="utf-8"), "partial-clone")
            self.assertFalse((root / "projects/agent-container/project.json").exists())

    def test_project_add_never_changes_existing_branch_markers(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            handovers = Path(temp) / "handovers"
            (handovers / "agent-container").mkdir(parents=True)
            self._authenticated_state(root)
            workspace = root / "workspaces/agent-container"
            (workspace / ".git").mkdir(parents=True)
            workspace.parent.chmod(0o700)
            branches = {
                "main": workspace / "BRANCH-main",
                "master": workspace / "BRANCH-master",
                "topic/keep-current": workspace / "BRANCH-topic",
            }
            for name, marker in branches.items():
                marker.write_text(name, encoding="utf-8")
            calls = []

            result = main(
                ["project", "add", "jj1xgo/agent-container", "--handover-root", str(handovers)],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=lambda spec: calls.append(spec) or subprocess.CompletedProcess(spec.argv, 0),
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
            )

            self.assertEqual(result, 0)
            for name, marker in branches.items():
                self.assertEqual(marker.read_text(encoding="utf-8"), name)
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
