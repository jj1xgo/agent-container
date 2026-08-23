from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import unittest
from unittest.mock import patch

from agent_container.agentctl import main
from agent_container.agentctl import parser
from agent_container.podman import run_codex_spec
from agent_container.state import ProjectRecord
from agent_container.state import Repository


def successful_podman_result(spec):
    if spec.argv == ("podman", "info", "--format", "{{.Host.Security.Rootless}}"):
        return subprocess.CompletedProcess(spec.argv, 0, stdout="true\n")
    return subprocess.CompletedProcess(spec.argv, 0, stdout="podman version 5.8\n")


class AgentCtlBuildAuthTest(unittest.TestCase):
    @staticmethod
    def _successful_probe(spec):
        return successful_podman_result(spec)

    def test_build_requires_successful_rootless_podman_before_build(self) -> None:
        calls = []

        def runner(spec):
            calls.append(spec)
            if spec.argv[:2] == ("podman", "info"):
                return subprocess.CompletedProcess(spec.argv, 0, stdout="false\n")
            return subprocess.CompletedProcess(
                spec.argv, 0, stdout="podman version 5.8\n"
            )

        result = main(["build"], runner=runner)

        self.assertEqual(result, 1)
        self.assertEqual(
            [call.argv for call in calls],
            [
                ("podman", "--version"),
                ("podman", "info", "--format", "{{.Host.Security.Rootless}}"),
            ],
        )

    def test_build_preserves_failed_version_probe_exit_code(self) -> None:
        calls = []
        stderr = StringIO()

        def runner(spec):
            calls.append(spec)
            return subprocess.CompletedProcess(spec.argv, 23)

        result = main(["build"], runner=runner, stderr=stderr)

        self.assertEqual(result, 23)
        self.assertEqual([call.argv for call in calls], [("podman", "--version")])
        self.assertEqual(stderr.getvalue(), "error: command failed with exit code 23\n")

    def test_invalid_image_is_rejected_before_any_runner_call(self) -> None:
        for image in ("", "-danger", "two words", "line\nfeed"):
            with self.subTest(image=image):
                calls = []
                result = main(
                    [f"--image={image}", "build"],
                    runner=lambda spec: calls.append(spec),
                )
                self.assertEqual(result, 1)
                self.assertEqual(calls, [])

    def test_build_uses_requested_versions_and_prints_ordered_cli_probes(self) -> None:
        calls = []
        stdout = StringIO()

        def runner(spec):
            calls.append(spec)
            if spec.argv[-2:] == ("codex", "--version"):
                return subprocess.CompletedProcess(spec.argv, 0, stdout="codex 0.149.0\n")
            if spec.argv[-2:] == ("claude", "--version"):
                return subprocess.CompletedProcess(spec.argv, 0, stdout="claude 1.2.3\n")
            return self._successful_probe(spec)

        result = main(
            ["build", "--codex-version", "0.149.0", "--claude-version", "1.2.3"],
            runner=runner,
            stdout=stdout,
            cachebuster_reader=lambda: "12345",
        )

        self.assertEqual(result, 0)
        self.assertIn("CODEX_VERSION=0.149.0", calls[2].argv)
        self.assertIn("CLAUDE_VERSION=1.2.3", calls[2].argv)
        self.assertIn("AGENT_CLI_CACHEBUST=12345", calls[2].argv)
        self.assertEqual(
            [call.argv[-2:] for call in calls[3:]],
            [("codex", "--version"), ("claude", "--version")],
        )
        self.assertEqual(
            stdout.getvalue(),
            "Codex version: codex 0.149.0\nClaude version: claude 1.2.3\n",
        )

    def test_build_returns_claude_probe_exit_without_printing_probe_stderr(self) -> None:
        calls = []
        stderr = StringIO()

        def runner(spec):
            calls.append(spec)
            if spec.argv[-2:] == ("claude", "--version"):
                return subprocess.CompletedProcess(
                    spec.argv,
                    23,
                    stderr="DO-NOT-PRINT-PROBE-STDERR",
                )
            return self._successful_probe(spec)

        result = main(
            ["build"],
            runner=runner,
            stderr=stderr,
            cachebuster_reader=lambda: "12345",
        )

        self.assertEqual(result, 23)
        self.assertEqual(calls[-1].argv[-2:], ("claude", "--version"))
        self.assertNotIn("DO-NOT-PRINT-PROBE-STDERR", stderr.getvalue())

    def test_auth_creates_private_state_and_runs_device_login(self) -> None:
        with TemporaryDirectory() as temp:
            calls = []
            environment = {"AGENT_CONTAINER_HOME": temp}

            def runner(spec):
                calls.append(spec)
                if spec.argv[-3:] == ("codex", "login", "--device-auth"):
                    auth_file = Path(temp) / "shared-auth/codex/auth.json"
                    auth_file.write_text("fixture-not-a-token", encoding="utf-8")
                    auth_file.chmod(0o600)
                return self._successful_probe(spec)

            result = main(["auth", "codex"], environment=environment, runner=runner)
            self.assertEqual(result, 0)
            self.assertEqual(
                (Path(temp) / "shared-auth/codex").stat().st_mode & 0o777, 0o700
            )
            self.assertEqual(len(calls), 5)
            self.assertIn("--device-auth", calls[3].argv)
            self.assertEqual(calls[4].argv[-3:], ("codex", "login", "status"))

    def test_auth_rejects_symlinked_state_root_before_probe_or_creation(self) -> None:
        with TemporaryDirectory() as temp:
            real_root = Path(temp) / "real-state"
            real_root.mkdir()
            linked_root = Path(temp) / "linked-state"
            linked_root.symlink_to(real_root, target_is_directory=True)
            calls = []

            result = main(
                ["auth", "codex"],
                environment={"AGENT_CONTAINER_HOME": str(linked_root)},
                runner=lambda spec: calls.append(spec),
            )

            self.assertEqual(result, 1)
            self.assertEqual(calls, [])
            self.assertEqual(list(real_root.iterdir()), [])

    def test_auth_propagates_login_status_failure(self) -> None:
        with TemporaryDirectory() as temp:
            calls = []

            def runner(spec):
                calls.append(spec)
                if spec.argv[-3:] == ("codex", "login", "--device-auth"):
                    auth_file = Path(temp) / "shared-auth/codex/auth.json"
                    auth_file.write_text(
                        "DO-NOT-PRINT-CREDENTIAL-BODY", encoding="utf-8"
                    )
                    auth_file.chmod(0o600)
                if spec.argv[-3:] == ("codex", "login", "status"):
                    return subprocess.CompletedProcess(spec.argv, 17)
                return self._successful_probe(spec)

            stderr = StringIO()
            result = main(
                ["auth", "codex"],
                environment={"AGENT_CONTAINER_HOME": temp},
                runner=runner,
                stderr=stderr,
            )

            self.assertEqual(result, 17)
            self.assertNotIn("DO-NOT-PRINT-CREDENTIAL-BODY", stderr.getvalue())

    def test_auth_missing_image_does_not_create_state(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "new-state"
            calls = []

            def runner(spec):
                calls.append(spec)
                if spec.argv[:3] == ("podman", "image", "exists"):
                    return subprocess.CompletedProcess(spec.argv, 31)
                return self._successful_probe(spec)

            result = main(
                ["auth", "codex"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=runner,
                stderr=StringIO(),
            )

            self.assertEqual(result, 31)
            self.assertEqual(len(calls), 3)
            self.assertFalse(root.exists())

    def test_auth_rejects_runner_created_broad_credential_file_before_status(self) -> None:
        with TemporaryDirectory() as temp:
            calls = []
            stderr = StringIO()

            def runner(spec):
                calls.append(spec)
                if spec.argv[-3:] == ("codex", "login", "--device-auth"):
                    auth_file = Path(temp) / "shared-auth/codex/auth.json"
                    auth_file.write_text(
                        "DO-NOT-PRINT-CREDENTIAL-BODY", encoding="utf-8"
                    )
                    auth_file.chmod(0o644)
                return self._successful_probe(spec)

            result = main(
                ["auth", "codex"],
                environment={"AGENT_CONTAINER_HOME": temp},
                runner=runner,
                stderr=stderr,
            )

            self.assertEqual(result, 1)
            self.assertEqual(len(calls), 4)
            self.assertIn("mode 0600", stderr.getvalue())
            self.assertNotIn("DO-NOT-PRINT-CREDENTIAL-BODY", stderr.getvalue())

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
                if "clone" in spec.argv:
                    workspace = root / "workspaces/agent-container"
                    workspace.mkdir(parents=True)
                    (workspace / ".git").mkdir()
                return successful_podman_result(spec)

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
            self.assertEqual(len(calls), 4)
            for forbidden in ("checkout", "reset", "clean", "fetch"):
                self.assertNotIn(forbidden, calls[-1].argv)

    def test_project_add_missing_image_never_starts_clone_or_creates_project_state(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            handovers = Path(temp) / "handovers"
            (handovers / "agent-container").mkdir(parents=True)
            self._authenticated_state(root)
            calls = []
            stderr = StringIO()

            def runner(spec):
                calls.append(spec)
                if spec.argv[:3] == ("podman", "image", "exists"):
                    return subprocess.CompletedProcess(spec.argv, 41)
                return successful_podman_result(spec)

            result = main(
                ["project", "add", "jj1xgo/agent-container", "--handover-root", str(handovers)],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=runner,
                stderr=stderr,
            )

            self.assertEqual(result, 41)
            self.assertEqual(len(calls), 3)
            self.assertFalse((root / "projects").exists())
            self.assertFalse((root / "workspaces").exists())

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
                if "clone" in spec.argv:
                    (root / "workspaces/agent-container/.git").mkdir(parents=True)
                return successful_podman_result(spec)

            result = main(
                ["project", "add", "jj1xgo/agent-container", "--handover-root", str(handovers)],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=runner,
                git_remote_reader=lambda path: "https://github.com/example/other.git",
            )

            self.assertEqual(result, 1)
            self.assertFalse((root / "projects/agent-container/project.json").exists())

    def test_project_add_rejects_git_symlink_before_reading_origin(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            handovers = Path(temp) / "handovers"
            (handovers / "agent-container").mkdir(parents=True)
            self._authenticated_state(root)
            workspace = root / "workspaces/agent-container"
            workspace.mkdir(parents=True)
            workspace.parent.chmod(0o700)
            git_target = Path(temp) / "git-target"
            git_target.mkdir()
            (workspace / ".git").symlink_to(git_target, target_is_directory=True)
            origin_reads = []

            result = main(
                ["project", "add", "jj1xgo/agent-container", "--handover-root", str(handovers)],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=lambda spec: self.fail("clone runner must not be called"),
                git_remote_reader=lambda path: origin_reads.append(path)
                or "https://github.com/jj1xgo/agent-container.git",
            )

            self.assertEqual(result, 1)
            self.assertEqual(origin_reads, [])
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

    def test_project_add_rejects_symlinked_state_root_before_probe_or_creation(self) -> None:
        with TemporaryDirectory() as temp:
            real_root = Path(temp) / "real-state"
            real_root.mkdir(mode=0o700)
            linked_root = Path(temp) / "linked-state"
            linked_root.symlink_to(real_root, target_is_directory=True)
            handovers = Path(temp) / "handovers"
            (handovers / "agent-container").mkdir(parents=True)
            calls = []

            result = main(
                ["project", "add", "jj1xgo/agent-container", "--handover-root", str(handovers)],
                environment={"AGENT_CONTAINER_HOME": str(linked_root)},
                runner=lambda spec: calls.append(spec),
            )

            self.assertEqual(result, 1)
            self.assertEqual(calls, [])
            self.assertEqual(list(real_root.iterdir()), [])

    def test_project_add_leaves_partial_clone_marker_after_failure(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            handovers = Path(temp) / "handovers"
            (handovers / "agent-container").mkdir(parents=True)
            self._authenticated_state(root)
            marker = root / "workspaces/agent-container/KEEP-ME"

            def runner(spec):
                if "clone" in spec.argv:
                    marker.parent.mkdir(parents=True)
                    marker.write_text("partial-clone", encoding="utf-8")
                    raise subprocess.CalledProcessError(9, spec.argv)
                return successful_podman_result(spec)

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
                runner=lambda spec: calls.append(spec) or successful_podman_result(spec),
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
            )

            self.assertEqual(result, 0)
            for name, marker in branches.items():
                self.assertEqual(marker.read_text(encoding="utf-8"), name)
            self.assertEqual(len(calls), 3)


class AgentCtlRunDoctorTest(unittest.TestCase):
    DOCTOR_CHECK_ORDER = [
        "podman-version",
        "podman-rootless",
        "image",
        "private-state",
        "codex-auth",
        "gh-hosts",
        "project-metadata",
        "workspace-origin",
        "handover-project",
        "network-policy",
    ]

    def _runtime_state(self, temp: str) -> tuple[Path, Path]:
        root = Path(temp) / "state"
        handover_root = Path(temp) / "handovers"
        private_directories = (
            root,
            root / "shared-auth",
            root / "shared-auth/codex",
            root / "gh",
            root / "projects",
            root / "projects/agent-container",
            root / "projects/agent-container/codex-home",
            root / "projects/agent-container/cache",
            root / "workspaces",
        )
        for directory in private_directories:
            directory.mkdir(mode=0o700)

        workspace = root / "workspaces/agent-container"
        (workspace / ".git").mkdir(parents=True)
        handover_project = handover_root / "agent-container"
        handover_project.mkdir(parents=True)

        auth_file = root / "shared-auth/codex/auth.json"
        auth_file.write_text("DO-NOT-PRINT-CREDENTIAL-BODY", encoding="utf-8")
        auth_file.chmod(0o600)
        hosts_file = root / "gh/hosts.yml"
        hosts_file.write_text("github.com:\n", encoding="utf-8")
        hosts_file.chmod(0o600)
        ProjectRecord(
            Repository.parse("jj1xgo/agent-container"), handover_root.resolve()
        ).write(root / "projects/agent-container/project.json")
        return root, handover_project

    def _assert_run_refused(
        self,
        root: Path,
        *,
        environment_root: Path | None = None,
        remote_url: str = "https://github.com/jj1xgo/agent-container.git",
    ) -> None:
        calls = []
        stdout = StringIO()
        stderr = StringIO()

        result = main(
            ["run", "agent-container"],
            environment={
                "AGENT_CONTAINER_HOME": str(
                    root if environment_root is None else environment_root
                )
            },
            runner=lambda spec: calls.append(spec)
            or subprocess.CompletedProcess(spec.argv, 0),
            git_remote_reader=lambda path: remote_url,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(result, 1)
        self.assertEqual(calls, [])
        rendered = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn("DO-NOT-PRINT-CREDENTIAL-BODY", rendered)

    def _successful_doctor_runner(self, spec):
        if spec.argv == ("podman", "info", "--format", "{{.Host.Security.Rootless}}"):
            return subprocess.CompletedProcess(spec.argv, 0, stdout="true\n")
        return subprocess.CompletedProcess(spec.argv, 0, stdout="podman version 5.8\n")

    def _doctor_check_names(self, rendered: str) -> list[str]:
        return [line.split()[1].removesuffix(":") for line in rendered.splitlines()]

    def test_run_validates_then_starts_codex(self) -> None:
        with TemporaryDirectory() as temp:
            root, handover_project = self._runtime_state(temp)
            calls = []
            output = StringIO()

            result = main(
                ["run", "agent-container"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=lambda spec: calls.append(spec) or successful_podman_result(spec),
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                stdout=output,
            )

            self.assertEqual(result, 0)
            self.assertEqual(len(calls), 4)
            self.assertIn("codex", calls[-1].argv)
            self.assertIn("AGENT_PROJECT_ID=agent-container", calls[-1].argv)
            self.assertIn(
                f"src={handover_project},dst=/handovers/agent-container",
                " ".join(calls[-1].argv),
            )
            self.assertEqual(
                output.getvalue(), "Starting Codex for project: agent-container\n"
            )
            self.assertNotIn(str(root / "shared-auth/codex/auth.json"), output.getvalue())
            self.assertEqual(os.getuid(), root.stat().st_uid)

    def test_run_missing_image_preserves_exit_code_without_starting_codex(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            calls = []
            stdout = StringIO()
            stderr = StringIO()

            def runner(spec):
                calls.append(spec)
                if spec.argv[:3] == ("podman", "image", "exists"):
                    return subprocess.CompletedProcess(spec.argv, 29)
                return successful_podman_result(spec)

            result = main(
                ["run", "agent-container"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=runner,
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(result, 29)
            self.assertEqual(len(calls), 3)
            self.assertNotIn("Starting Codex", stdout.getvalue())
            self.assertNotIn("DO-NOT-PRINT-CREDENTIAL-BODY", stderr.getvalue())

    def test_run_rejects_identity_mismatch_before_building_runtime_spec(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            builder_calls = []
            runner_calls = []
            stderr = StringIO()

            def runtime_spec_builder(*args):
                builder_calls.append(args)
                return run_codex_spec(*args)

            result = main(
                ["run", "agent-container"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=lambda spec: runner_calls.append(spec)
                or subprocess.CompletedProcess(spec.argv, 0),
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                identity_reader=lambda: (os.getuid() + 1, os.getgid()),
                runtime_spec_builder=runtime_spec_builder,
                stderr=stderr,
            )

            self.assertEqual(result, 1)
            self.assertEqual(builder_calls, [])
            self.assertEqual(runner_calls, [])
            self.assertIn("must match the current process", stderr.getvalue())

    def test_doctor_reports_presence_without_secret_values(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            calls = []
            output = StringIO()

            def doctor_runner(spec):
                calls.append(spec)
                if spec.argv == ("podman", "info", "--format", "{{.Host.Security.Rootless}}"):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="true\n")
                return subprocess.CompletedProcess(spec.argv, 0, stdout="podman version 5.8\n")

            result = main(
                ["doctor", "agent-container"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=doctor_runner,
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                stdout=output,
            )

            self.assertEqual(result, 0)
            rendered = output.getvalue()
            self.assertIn("PASS  podman-rootless: true", rendered)
            self.assertIn("PASS  codex-auth: present, mode 0600", rendered)
            self.assertIn("PASS  gh-hosts: present, mode 0600", rendered)
            self.assertIn("PASS  workspace-origin: exact HTTPS origin", rendered)
            self.assertIn(
                "WARN  network-policy: outbound network is not domain-restricted in Phase 1",
                rendered,
            )
            self.assertNotIn("DO-NOT-PRINT-CREDENTIAL-BODY", rendered)
            self.assertEqual(
                [call.argv for call in calls],
                [
                    ("podman", "--version"),
                    ("podman", "info", "--format", "{{.Host.Security.Rootless}}"),
                    ("podman", "image", "exists", "localhost/agent-container:dev"),
                ],
            )
            self.assertEqual(
                self._doctor_check_names(rendered),
                self.DOCTOR_CHECK_ORDER,
            )

    def test_doctor_failure_is_nonzero_and_does_not_print_secret_values(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            (root / "shared-auth/codex/auth.json").chmod(0o644)
            output = StringIO()

            def doctor_runner(spec):
                if spec.argv == ("podman", "info", "--format", "{{.Host.Security.Rootless}}"):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="true\n")
                return subprocess.CompletedProcess(spec.argv, 0)

            result = main(
                ["doctor", "agent-container"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=doctor_runner,
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                stdout=output,
            )

            self.assertEqual(result, 1)
            self.assertIn("FAIL  codex-auth:", output.getvalue())
            self.assertIn("WARN  network-policy:", output.getvalue())
            self.assertNotIn("DO-NOT-PRINT-CREDENTIAL-BODY", output.getvalue())

    def test_doctor_converts_filesystem_oserrors_to_ordered_failures(self) -> None:
        cases = (
            ("private-state", "agent_container.agentctl._ensure_exact_state_root"),
            ("project-metadata", "agent_container.agentctl._read_runtime_project"),
            ("handover-project", "agent_container.agentctl._resolve_handover_root"),
        )
        for failed_check, target in cases:
            with self.subTest(check=failed_check), TemporaryDirectory() as temp:
                root, _ = self._runtime_state(temp)
                output = StringIO()
                errors = StringIO()
                with patch(
                    target,
                    side_effect=OSError("DO-NOT-PRINT-CREDENTIAL-BODY"),
                ):
                    result = main(
                        ["doctor", "agent-container"],
                        environment={"AGENT_CONTAINER_HOME": str(root)},
                        runner=self._successful_doctor_runner,
                        git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                        stdout=output,
                        stderr=errors,
                    )

                rendered = output.getvalue() + errors.getvalue()
                self.assertEqual(result, 1)
                self.assertIn(f"FAIL  {failed_check}: filesystem operation failed", rendered)
                self.assertEqual(
                    self._doctor_check_names(rendered),
                    self.DOCTOR_CHECK_ORDER,
                )
                self.assertNotIn("DO-NOT-PRINT-CREDENTIAL-BODY", rendered)

    def test_run_converts_filesystem_oserror_without_running_container(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            runner_calls = []
            stderr = StringIO()

            def failing_remote_reader(path):
                raise OSError("DO-NOT-PRINT-CREDENTIAL-BODY")

            result = main(
                ["run", "agent-container"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=lambda spec: runner_calls.append(spec)
                or subprocess.CompletedProcess(spec.argv, 0),
                git_remote_reader=failing_remote_reader,
                stderr=stderr,
            )

            self.assertEqual(result, 1)
            self.assertEqual(runner_calls, [])
            self.assertIn("error: filesystem operation failed", stderr.getvalue())
            self.assertNotIn("DO-NOT-PRINT-CREDENTIAL-BODY", stderr.getvalue())

    def test_run_rejects_symlinked_configured_state_root_before_runner(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            linked_root = Path(temp) / "linked-state"
            linked_root.symlink_to(root, target_is_directory=True)

            self._assert_run_refused(root, environment_root=linked_root)

    def test_run_rejects_symlinked_state_root_ancestor_before_runner(self) -> None:
        with TemporaryDirectory() as temp:
            real_parent = Path(temp) / "real-parent"
            real_parent.mkdir()
            root, _ = self._runtime_state(str(real_parent))
            linked_parent = Path(temp) / "linked-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)

            self._assert_run_refused(
                root,
                environment_root=linked_parent / "state",
            )

    def test_run_rejects_symlinked_metadata_handover_root_before_runner(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            real_root = Path(temp) / "real-handovers"
            (real_root / "agent-container").mkdir(parents=True)
            linked_root = Path(temp) / "linked-handovers"
            linked_root.symlink_to(real_root, target_is_directory=True)
            project_file = root / "projects/agent-container/project.json"
            project_file.write_text(
                json.dumps(
                    {
                        "repository": "jj1xgo/agent-container",
                        "handover_root": str(linked_root),
                    }
                ),
                encoding="utf-8",
            )
            project_file.chmod(0o600)

            self._assert_run_refused(root)

    def test_run_rejects_symlinked_auth_file_before_runner(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            auth_file = root / "shared-auth/codex/auth.json"
            auth_target = Path(temp) / "outside-auth.json"
            auth_target.write_text("DO-NOT-PRINT-CREDENTIAL-BODY", encoding="utf-8")
            auth_target.chmod(0o600)
            auth_file.unlink()
            auth_file.symlink_to(auth_target)

            self._assert_run_refused(root)

    def test_run_rejects_symlinked_gh_directory_before_runner(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            gh_dir = root / "gh"
            (gh_dir / "hosts.yml").unlink()
            gh_dir.rmdir()
            gh_target = Path(temp) / "outside-gh"
            gh_target.mkdir(mode=0o700)
            hosts = gh_target / "hosts.yml"
            hosts.write_text("github.com:\n", encoding="utf-8")
            hosts.chmod(0o600)
            gh_dir.symlink_to(gh_target, target_is_directory=True)

            self._assert_run_refused(root)

    def test_run_rejects_symlinked_workspace_before_runner(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            workspace = root / "workspaces/agent-container"
            (workspace / ".git").rmdir()
            workspace.rmdir()
            workspace_target = Path(temp) / "outside-workspace"
            (workspace_target / ".git").mkdir(parents=True)
            workspace.symlink_to(workspace_target, target_is_directory=True)

            self._assert_run_refused(root)

    def test_run_rejects_symlinked_git_directory_before_runner(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            git_directory = root / "workspaces/agent-container/.git"
            git_directory.rmdir()
            git_target = Path(temp) / "outside-git"
            git_target.mkdir()
            git_directory.symlink_to(git_target, target_is_directory=True)

            self._assert_run_refused(root)

    def test_run_rejects_symlinked_project_metadata_before_runner(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            project_file = root / "projects/agent-container/project.json"
            metadata = project_file.read_text(encoding="utf-8")
            project_file.unlink()
            metadata_target = Path(temp) / "outside-project.json"
            metadata_target.write_text(metadata, encoding="utf-8")
            metadata_target.chmod(0o600)
            project_file.symlink_to(metadata_target)

            self._assert_run_refused(root)

    def test_run_rejects_malformed_project_metadata_before_runner(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            project_file = root / "projects/agent-container/project.json"
            project_file.write_text(
                json.dumps(
                    {
                        "repository": ["jj1xgo", "agent-container"],
                        "handover_root": {"path": "/tmp/handovers"},
                    }
                ),
                encoding="utf-8",
            )
            project_file.chmod(0o600)

            self._assert_run_refused(root)

    def test_run_rejects_symlinked_handover_project_before_runner(self) -> None:
        with TemporaryDirectory() as temp:
            root, handover_project = self._runtime_state(temp)
            handover_project.rmdir()
            handover_target = Path(temp) / "outside-handover"
            handover_target.mkdir()
            handover_project.symlink_to(handover_target, target_is_directory=True)

            self._assert_run_refused(root)

    def test_run_rejects_broad_private_modes_before_runner(self) -> None:
        private_paths = (
            ("state-root", "state", 0o755),
            ("shared-auth-parent", "state/shared-auth", 0o755),
            ("codex-auth-directory", "state/shared-auth/codex", 0o755),
            ("gh-directory", "state/gh", 0o755),
            ("projects-parent", "state/projects", 0o755),
            ("project-directory", "state/projects/agent-container", 0o755),
            ("codex-home", "state/projects/agent-container/codex-home", 0o755),
            ("cache", "state/projects/agent-container/cache", 0o755),
            ("workspaces-parent", "state/workspaces", 0o755),
            ("codex-auth", "state/shared-auth/codex/auth.json", 0o644),
            ("gh-hosts", "state/gh/hosts.yml", 0o644),
            ("project-metadata", "state/projects/agent-container/project.json", 0o644),
        )
        for name, relative_path, mode in private_paths:
            with self.subTest(boundary=name), TemporaryDirectory() as temp:
                root, _ = self._runtime_state(temp)
                (Path(temp) / relative_path).chmod(mode)

                self._assert_run_refused(root)

    def test_run_rejects_mismatched_workspace_origin_before_runner(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)

            self._assert_run_refused(
                root,
                remote_url="https://github.com/example/other.git",
            )


class AgentCtlParserTest(unittest.TestCase):
    def test_new_command_contract(self) -> None:
        build = parser().parse_args(["build"])
        self.assertEqual((build.codex_version, build.claude_version), ("latest", "latest"))
        self.assertEqual(parser().parse_args(["run", "p"]).agent, "codex")
        self.assertEqual(
            parser().parse_args(["run", "p", "--agent", "claude"]).agent,
            "claude",
        )
        self.assertEqual(
            parser().parse_args(["doctor", "p", "--agent", "all"]).agent,
            "all",
        )
        migrate = parser().parse_args(
            ["migrate", "claude", "p", "--from", "/old/.claude",
             "--plugin", "issue-ops@local-marketplace"]
        )
        self.assertEqual(migrate.source, Path("/old/.claude"))
        self.assertEqual(migrate.plugins, ["issue-ops@local-marketplace"])
        self.assertFalse(migrate.apply)


if __name__ == "__main__":
    unittest.main()
