from io import StringIO
import hmac
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import agent_container.agentctl as agentctl
from agent_container import __version__
from agent_container.agentctl import main
from agent_container.agentctl import parser
from agent_container.handover_broker_runtime import HandoverBrokerRuntimeError
from agent_container.handover_broker_runtime import HandoverRuntimeMount
from agent_container.podman import CommandSpec
from agent_container.podman import run_codex_spec
from agent_container.podman import run_claude_spec
from agent_container.podman import BrokerRuntimeMount
from agent_container.podman import podman_running_agent_containers_spec
from agent_container.podman import podman_stats_spec
from agent_container.project_image import ProjectImageConfig
from agent_container.project_image import project_image_key
from agent_container.project_image import project_image_name
from agent_container.profile import seed_codex_home
from agent_container.state import ProjectRecord
from agent_container.state import Repository


def successful_podman_result(spec):
    if spec.argv == ("podman", "info", "--format", "{{.Host.Security.Rootless}}"):
        return subprocess.CompletedProcess(spec.argv, 0, stdout="true\n")
    return subprocess.CompletedProcess(spec.argv, 0, stdout="podman version 5.8\n")


class _TrackingBrokerContext:
    def __init__(self, name, mount, events, on_enter=None, enter_error=None):
        self.name = name
        self.mount = mount
        self.events = events
        self.on_enter = on_enter
        self.enter_error = enter_error
        self.active = False

    def __enter__(self):
        self.events.append(f"{self.name}-enter")
        if self.on_enter is not None:
            self.on_enter()
        if self.enter_error is not None:
            raise self.enter_error
        self.active = True
        return self.mount

    def __exit__(self, *_):
        self.events.append(f"{self.name}-exit")
        self.active = False


class AgentCtlBuildAuthTest(unittest.TestCase):
    OLD_TOKEN = "o" * 32
    NEW_TOKEN = "n" * 32

    def test_stats_reports_only_fixed_resource_fields_for_matching_agents(self) -> None:
        stdout = StringIO()

        def runner(spec):
            if spec.argv == ("podman", "--version"):
                return subprocess.CompletedProcess(spec.argv, 0, stdout="podman version 5.8\n")
            if spec.argv[:2] == ("podman", "info"):
                return subprocess.CompletedProcess(spec.argv, 0, stdout="true\n")
            if spec == podman_running_agent_containers_spec("agent-container", "codex"):
                return subprocess.CompletedProcess(spec.argv, 0, stdout="0123456789ab\n")
            if spec == podman_running_agent_containers_spec("agent-container", "claude"):
                return subprocess.CompletedProcess(spec.argv, 0, stdout="abcdef012345\n")
            if spec == podman_stats_spec("0123456789ab"):
                return subprocess.CompletedProcess(
                    spec.argv, 0, stdout="0123456789ab\t1.25%\t10MiB / 1GiB\t8\t2m\n"
                )
            if spec == podman_stats_spec("abcdef012345"):
                return subprocess.CompletedProcess(
                    spec.argv, 0, stdout="abcdef012345\t0.50%\t20MiB / 1GiB\t9\t3m\n"
                )
            raise AssertionError(spec.argv)

        result = main(["stats", "agent-container"], runner=runner, stdout=stdout)

        self.assertEqual(result, 0)
        self.assertEqual(
            stdout.getvalue(),
            "AGENT\tCONTAINER\tCPU\tMEMORY\tPIDS\tUPTIME\n"
            "codex\t0123456789ab\t1.25%\t10MiB / 1GiB\t8\t2m\n"
            "claude\tabcdef012345\t0.50%\t20MiB / 1GiB\t9\t3m\n",
        )

    def test_stats_fails_closed_when_no_runtime_or_output_is_invalid(self) -> None:
        def no_runtime(spec):
            if spec.argv == ("podman", "--version"):
                return subprocess.CompletedProcess(spec.argv, 0, stdout="podman version 5.8\n")
            if spec.argv[:2] == ("podman", "info"):
                return subprocess.CompletedProcess(spec.argv, 0, stdout="true\n")
            return subprocess.CompletedProcess(spec.argv, 0, stdout="")

        stderr = StringIO()
        result = main(
            ["stats", "agent-container"], runner=no_runtime, stderr=stderr
        )
        self.assertEqual(result, 1)
        self.assertIn("no running agent container", stderr.getvalue())

        calls = []
        result = main(
            ["stats", "../bad"], runner=lambda spec: calls.append(spec)
        )
        self.assertEqual(result, 1)
        self.assertEqual(calls, [])

    def test_stats_rejects_malformed_podman_output_without_echoing_it(self) -> None:
        marker = "private-command-marker"

        def runner(spec):
            if spec.argv == ("podman", "--version"):
                return subprocess.CompletedProcess(spec.argv, 0, stdout="podman version 5.8\n")
            if spec.argv[:2] == ("podman", "info"):
                return subprocess.CompletedProcess(spec.argv, 0, stdout="true\n")
            if spec == podman_running_agent_containers_spec("agent-container", "codex"):
                return subprocess.CompletedProcess(spec.argv, 0, stdout="0123456789ab\n")
            if spec == podman_running_agent_containers_spec("agent-container", "claude"):
                return subprocess.CompletedProcess(spec.argv, 0, stdout="")
            return subprocess.CompletedProcess(
                spec.argv,
                0,
                stdout=f"0123456789ab\t1%\t2MiB\t3\t4m\t{marker}\n",
            )

        stdout = StringIO()
        stderr = StringIO()
        result = main(
            ["stats", "agent-container"],
            runner=runner,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(result, 1)
        self.assertNotIn(marker, stdout.getvalue())
        self.assertNotIn(marker, stderr.getvalue())

    @staticmethod
    def _successful_probe(spec):
        return successful_podman_result(spec)

    def _make_claude_state(
        self, root: Path, token: str | None = None, legacy: bool = False
    ) -> Path:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        shared = root / "shared-auth"
        shared.mkdir(exist_ok=True, mode=0o700)
        shared.chmod(0o700)
        auth_dir = shared / "claude"
        auth_dir.mkdir(exist_ok=True, mode=0o700)
        auth_dir.chmod(0o700)
        if token is not None:
            active = auth_dir / "oauth-token"
            active.write_text(token, encoding="ascii")
            active.chmod(0o600)
        if legacy:
            for name, body in (
                (".credentials.json", "legacy-credential-private"),
                (".claude.json", "legacy-metadata-private"),
            ):
                path = auth_dir / name
                path.write_text(body, encoding="ascii")
                path.chmod(0o600)
            backups = auth_dir / "backups"
            backups.mkdir(mode=0o700)
            backup = backups / "old"
            backup.write_text("legacy-backup-private", encoding="ascii")
            backup.chmod(0o600)
        return auth_dir

    def _assert_token_matches(self, path: Path, expected: bytes) -> None:
        self.assertTrue(hmac.compare_digest(path.read_bytes(), expected))

    def _assert_private_values_absent(
        self, rendered: str, *private_values: str
    ) -> None:
        self.assertTrue(all(value not in rendered for value in private_values))

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

    def test_build_rejects_invalid_node_version_before_any_runner_call(self) -> None:
        calls = []
        stderr = StringIO()

        result = main(
            ["build", "--node-version", "../bad"],
            runner=lambda spec: calls.append(spec),
            stderr=stderr,
        )

        self.assertEqual(result, 1)
        self.assertEqual(calls, [])
        self.assertIn("version", stderr.getvalue())

    def test_build_uses_requested_versions_and_prints_ordered_cli_probes(self) -> None:
        calls = []
        stdout = StringIO()

        def runner(spec):
            calls.append(spec)
            if spec.argv[-2:] == ("/opt/agent-node/bin/node", "--version"):
                return subprocess.CompletedProcess(spec.argv, 0, stdout="v22.23.1\n")
            if spec.argv[-2:] == ("codex", "--version"):
                return subprocess.CompletedProcess(spec.argv, 0, stdout="codex 0.149.0\n")
            if spec.argv[-2:] == ("claude", "--version"):
                return subprocess.CompletedProcess(spec.argv, 0, stdout="claude 1.2.3\n")
            return self._successful_probe(spec)

        result = main(
            [
                "build",
                "--node-version",
                "22.23.1",
                "--codex-version",
                "0.149.0",
                "--claude-version",
                "1.2.3",
            ],
            runner=runner,
            stdout=stdout,
            cachebuster_reader=lambda: "12345",
        )

        self.assertEqual(result, 0)
        self.assertIn("NODE_VERSION=22.23.1", calls[2].argv)
        self.assertIn("CODEX_VERSION=0.149.0", calls[2].argv)
        self.assertIn("CLAUDE_VERSION=1.2.3", calls[2].argv)
        self.assertIn("AGENT_CLI_CACHEBUST=12345", calls[2].argv)
        self.assertIn(f"AGENT_CONTAINER_VERSION={__version__}", calls[2].argv)
        self.assertEqual(
            [call.argv[-2:] for call in calls[3:]],
            [
                ("/opt/agent-node/bin/node", "--version"),
                ("codex", "--version"),
                ("claude", "--version"),
            ],
        )
        self.assertEqual(
            stdout.getvalue(),
            "Node version: v22.23.1\n"
            "Codex version: codex 0.149.0\n"
            "Claude version: claude 1.2.3\n",
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

    def test_claude_auth_activates_verified_token_and_quarantines_legacy(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            auth_dir = self._make_claude_state(root, self.OLD_TOKEN, legacy=True)
            active = auth_dir / "oauth-token"
            calls = []
            status_observations = []
            stdout = StringIO()
            stderr = StringIO()

            def runner(spec):
                calls.append(spec)
                if spec.argv[-3:] == ("claude", "auth", "status"):
                    status_observations.append(
                        (
                            hmac.compare_digest(active.read_bytes(), b"o" * 32),
                            (auth_dir / ".credentials.json").exists(),
                            (auth_dir / ".claude.json").exists(),
                            (auth_dir / "backups").exists(),
                        )
                    )
                    return subprocess.CompletedProcess(
                        spec.argv,
                        0,
                        stdout="status-output-private",
                        stderr="runner-stderr-private",
                    )
                return self._successful_probe(spec)

            result = main(
                ["auth", "claude"],
                environment={"AGENT_CONTAINER_HOME": temp},
                runner=runner,
                token_reader=lambda prompt: self.NEW_TOKEN,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(result, 0)
            self.assertEqual(len(calls), 5)
            self.assertEqual(calls[0].argv, ("podman", "--version"))
            self.assertEqual(calls[1].argv[:2], ("podman", "info"))
            self.assertEqual(calls[2].argv[:3], ("podman", "image", "exists"))
            self.assertEqual(calls[3].argv[-2:], ("claude", "setup-token"))
            self.assertEqual(calls[4].argv[-3:], ("claude", "auth", "status"))
            status_mount = next(
                value
                for value in calls[4].argv
                if "dst=/run/secrets/claude-oauth-token" in value
            )
            staged_source = Path(
                status_mount.split("src=", 1)[1].split(",dst=", 1)[0]
            )
            self.assertEqual(staged_source.parent, auth_dir)
            self.assertNotEqual(staged_source, active)
            self.assertFalse(staged_source.exists())
            self.assertEqual(status_observations, [(True, True, True, True)])
            self._assert_token_matches(active, b"n" * 32)
            quarantine_runs = list((root / "quarantine/claude").iterdir())
            self.assertEqual(len(quarantine_runs), 1)
            self.assertEqual(
                {path.name for path in quarantine_runs[0].iterdir()},
                {".credentials.json", ".claude.json", "backups"},
            )
            self.assertFalse((quarantine_runs[0] / "oauth-token").exists())
            rendered = stdout.getvalue() + stderr.getvalue()
            self._assert_private_values_absent(
                rendered,
                self.NEW_TOKEN,
                self.OLD_TOKEN,
                "legacy-credential-private",
                "legacy-metadata-private",
                "legacy-backup-private",
                "status-output-private",
                "runner-stderr-private",
            )

    def test_claude_auth_setup_failure_does_not_prompt_or_change_existing_token(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._make_claude_state(root, self.OLD_TOKEN) / "oauth-token"
            old_bytes = active.read_bytes()
            calls = []

            def runner(spec):
                calls.append(spec)
                if spec.argv[-2:] == ("claude", "setup-token"):
                    return subprocess.CompletedProcess(spec.argv, 19)
                return self._successful_probe(spec)

            result = main(
                ["auth", "claude"],
                environment={"AGENT_CONTAINER_HOME": temp},
                runner=runner,
                token_reader=lambda prompt: (_ for _ in ()).throw(AssertionError()),
                stderr=StringIO(),
            )

            self.assertEqual(result, 19)
            self.assertEqual(calls[-1].argv[-2:], ("claude", "setup-token"))
            self._assert_token_matches(active, old_bytes)
            self.assertEqual(list(active.parent.glob(".oauth-token-stage-*")), [])

    def test_claude_auth_cancelled_prompt_preserves_existing_token(self) -> None:
        for cancellation in (EOFError, KeyboardInterrupt):
            with self.subTest(cancellation=cancellation.__name__), TemporaryDirectory() as temp:
                root = Path(temp)
                active = self._make_claude_state(root, self.OLD_TOKEN) / "oauth-token"
                old_bytes = active.read_bytes()
                calls = []
                stdout = StringIO()
                stderr = StringIO()

                def reader(prompt):
                    raise cancellation

                result = main(
                    ["auth", "claude"],
                    environment={"AGENT_CONTAINER_HOME": temp},
                    runner=lambda spec: calls.append(spec) or self._successful_probe(spec),
                    token_reader=reader,
                    stdout=stdout,
                    stderr=stderr,
                )

                self.assertEqual(result, 1)
                self.assertEqual(calls[-1].argv[-2:], ("claude", "setup-token"))
                self.assertEqual(stderr.getvalue(), "error: Claude token input cancelled\n")
                self._assert_token_matches(active, old_bytes)
                self.assertEqual(list(active.parent.glob(".oauth-token-stage-*")), [])
                self._assert_private_values_absent(
                    stdout.getvalue() + stderr.getvalue(), self.OLD_TOKEN
                )

    def test_claude_auth_invalid_token_never_runs_status_and_preserves_existing(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._make_claude_state(root, self.OLD_TOKEN) / "oauth-token"
            old_bytes = active.read_bytes()
            calls = []
            stderr = StringIO()

            result = main(
                ["auth", "claude"],
                environment={"AGENT_CONTAINER_HOME": temp},
                runner=lambda spec: calls.append(spec) or self._successful_probe(spec),
                token_reader=lambda prompt: "z" * 31,
                stderr=stderr,
            )

            self.assertEqual(result, 1)
            self.assertTrue(all(call.argv[-3:] != ("claude", "auth", "status") for call in calls))
            self._assert_token_matches(active, old_bytes)
            self.assertEqual(list(active.parent.glob(".oauth-token-stage-*")), [])
            self._assert_private_values_absent(
                stderr.getvalue(), "z" * 31, self.OLD_TOKEN
            )

    def test_claude_auth_status_failure_discards_stage_and_preserves_existing(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._make_claude_state(root, self.OLD_TOKEN) / "oauth-token"
            old_bytes = active.read_bytes()
            calls = []
            stdout = StringIO()
            stderr = StringIO()

            def runner(spec):
                calls.append(spec)
                if spec.argv[-3:] == ("claude", "auth", "status"):
                    return subprocess.CompletedProcess(
                        spec.argv,
                        17,
                        stdout="status-output-private",
                        stderr="runner-stderr-private",
                    )
                return self._successful_probe(spec)

            result = main(
                ["auth", "claude"],
                environment={"AGENT_CONTAINER_HOME": temp},
                runner=runner,
                token_reader=lambda prompt: self.NEW_TOKEN,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(result, 17)
            self._assert_token_matches(active, old_bytes)
            self.assertEqual(list(active.parent.glob(".oauth-token-stage-*")), [])
            self._assert_private_values_absent(
                stdout.getvalue() + stderr.getvalue(),
                self.NEW_TOKEN,
                self.OLD_TOKEN,
                "status-output-private",
                "runner-stderr-private",
            )

    def test_claude_auth_missing_image_creates_no_state_and_does_not_prompt(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "new-state"
            calls = []

            def runner(spec):
                calls.append(spec)
                if spec.argv[:3] == ("podman", "image", "exists"):
                    return subprocess.CompletedProcess(spec.argv, 31)
                return self._successful_probe(spec)

            result = main(
                ["auth", "claude"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=runner,
                token_reader=lambda prompt: (_ for _ in ()).throw(AssertionError()),
                stderr=StringIO(),
            )

            self.assertEqual(result, 31)
            self.assertEqual(len(calls), 3)
            self.assertFalse(root.exists())

    def test_claude_auth_rejects_unsafe_existing_token_metadata_before_container(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._make_claude_state(root, self.OLD_TOKEN) / "oauth-token"
            active.chmod(0o644)
            old_bytes = active.read_bytes()
            calls = []
            stderr = StringIO()

            result = main(
                ["auth", "claude"],
                environment={"AGENT_CONTAINER_HOME": temp},
                runner=lambda spec: calls.append(spec),
                token_reader=lambda prompt: self.NEW_TOKEN,
                stderr=stderr,
            )

            self.assertEqual(result, 1)
            self.assertEqual(calls, [])
            self._assert_token_matches(active, old_bytes)
            self.assertEqual(stderr.getvalue(), "error: Claude token filesystem operation failed\n")
            self._assert_private_values_absent(
                stderr.getvalue(), str(active), self.OLD_TOKEN, self.NEW_TOKEN
            )

    def test_claude_auth_allows_replacing_malformed_existing_token(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._make_claude_state(root, "malformed") / "oauth-token"

            result = main(
                ["auth", "claude"],
                environment={"AGENT_CONTAINER_HOME": temp},
                runner=self._successful_probe,
                token_reader=lambda prompt: self.NEW_TOKEN,
                stderr=StringIO(),
            )

            self.assertEqual(result, 0)
            self._assert_token_matches(active, b"n" * 32)

    def test_claude_auth_requires_private_tty_for_default_reader(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._make_claude_state(root, self.OLD_TOKEN) / "oauth-token"
            old_bytes = active.read_bytes()
            calls = []
            stderr = StringIO()

            with patch(
                "agent_container.agentctl.getpass.getpass", side_effect=AssertionError
            ):
                result = main(
                    ["auth", "claude"],
                    environment={"AGENT_CONTAINER_HOME": temp},
                    runner=lambda spec: calls.append(spec) or self._successful_probe(spec),
                    stdin=StringIO(),
                    stderr=stderr,
                )

            self.assertEqual(result, 1)
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(call.argv[-2:] != ("claude", "setup-token") for call in calls))
            self._assert_token_matches(active, old_bytes)

    def test_claude_auth_status_output_is_suppressed_with_real_run_command_adapter(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            bin_dir = Path(temp) / "bin"
            bin_dir.mkdir()
            podman = bin_dir / "podman"
            podman.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  'info --format {{.Host.Security.Rootless}}') echo true ;;\n"
                "  *'auth status') echo status-output-private; "
                "echo runner-stderr-private >&2 ;;\n"
                "esac\n",
                encoding="ascii",
            )
            podman.chmod(0o755)
            stdout = StringIO()
            stderr = StringIO()

            with patch.dict(os.environ, {"PATH": str(bin_dir)}):
                result = main(
                    ["auth", "claude"],
                    environment={"AGENT_CONTAINER_HOME": str(root)},
                    token_reader=lambda prompt: self.NEW_TOKEN,
                    stdout=stdout,
                    stderr=stderr,
                )

            self.assertEqual(result, 0)
            rendered = stdout.getvalue() + stderr.getvalue()
            self._assert_private_values_absent(
                rendered,
                self.NEW_TOKEN,
                "status-output-private",
                "runner-stderr-private",
            )

    def test_claude_auth_validates_legacy_sources_before_setup_and_moves_after_status(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            auth_dir = self._make_claude_state(root, self.OLD_TOKEN, legacy=True)
            active = auth_dir / "oauth-token"
            order = []

            def runner(spec):
                if spec.argv[-2:] == ("claude", "setup-token"):
                    order.append("setup-with-legacy")
                    self.assertTrue((auth_dir / ".credentials.json").exists())
                if spec.argv[-3:] == ("claude", "auth", "status"):
                    order.append("status-with-legacy-and-old-active")
                    self.assertTrue((auth_dir / ".credentials.json").exists())
                    self._assert_token_matches(active, b"o" * 32)
                return self._successful_probe(spec)

            result = main(
                ["auth", "claude"],
                environment={"AGENT_CONTAINER_HOME": temp},
                runner=runner,
                token_reader=lambda prompt: self.NEW_TOKEN,
                stderr=StringIO(),
            )

            self.assertEqual(result, 0)
            self.assertEqual(
                order,
                ["setup-with-legacy", "status-with-legacy-and-old-active"],
            )
            self.assertFalse((auth_dir / ".credentials.json").exists())
            self.assertTrue(any((root / "quarantine/claude").iterdir()))

    def test_claude_auth_staging_failure_preserves_token_without_path_leak(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            auth_dir = self._make_claude_state(root, self.OLD_TOKEN)
            active = auth_dir / "oauth-token"
            old_bytes = active.read_bytes()
            stderr = StringIO()

            def reader(prompt):
                auth_dir.chmod(0o755)
                return self.NEW_TOKEN

            result = main(
                ["auth", "claude"],
                environment={"AGENT_CONTAINER_HOME": temp},
                runner=self._successful_probe,
                token_reader=reader,
                stderr=stderr,
            )

            self.assertEqual(result, 1)
            self._assert_token_matches(active, old_bytes)
            self.assertEqual(stderr.getvalue(), "error: Claude token filesystem operation failed\n")
            self._assert_private_values_absent(
                stderr.getvalue(), str(auth_dir), self.NEW_TOKEN, self.OLD_TOKEN
            )


class AgentCtlProjectTest(unittest.TestCase):
    def test_superpowers_update_refreshes_upstream_codex_and_official_claude(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            project_dir = root / "projects/agent-container"
            codex_home = project_dir / "codex-home"
            claude_config = project_dir / "claude-config"
            for directory in (
                root,
                root / "projects",
                project_dir,
                codex_home,
                claude_config,
            ):
                directory.mkdir(exist_ok=True, mode=0o700)
                directory.chmod(0o700)
            (codex_home / ".tmp/marketplaces/superpowers-dev").mkdir(
                parents=True, mode=0o700
            )
            (claude_config / "plugins/marketplaces/claude-plugins-official").mkdir(
                parents=True, mode=0o700
            )
            (claude_config / "plugins/cache/claude-plugins-official/superpowers").mkdir(
                parents=True, mode=0o700
            )
            ProjectRecord(
                Repository.parse("jj1xgo/agent-container"),
                Path(temp) / "handovers",
            ).write(project_dir / "project.json")
            calls = []
            stdout = StringIO()

            result = main(
                ["superpowers", "update", "agent-container"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=lambda spec: calls.append(spec) or successful_podman_result(spec),
                stdout=stdout,
            )

            self.assertEqual(result, 0)
            joined = [" ".join(call.argv) for call in calls]
            self.assertTrue(any("marketplace upgrade superpowers-dev" in call for call in joined))
            self.assertTrue(any("plugin add superpowers@superpowers-dev" in call for call in joined))
            self.assertTrue(any("plugin update superpowers@claude-plugins-official" in call for call in joined))
            self.assertIn("Updated Superpowers", stdout.getvalue())

    def test_superpowers_update_all_projects_updates_each_registered_project(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            root.mkdir(mode=0o700)
            projects = root / "projects"
            projects.mkdir(mode=0o700)
            for project_id in ("alpha", "beta"):
                project_dir = projects / project_id
                project_dir.mkdir(mode=0o700)
                (project_dir / "codex-home").mkdir(mode=0o700)
                (project_dir / "claude-config").mkdir(mode=0o700)
                ProjectRecord(
                    Repository.parse(f"owner/{project_id}"),
                    Path(temp) / "handovers",
                ).write(project_dir / "project.json")
            calls = []
            stdout = StringIO()

            result = main(
                ["superpowers", "update", "--all-projects"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=lambda spec: calls.append(spec) or successful_podman_result(spec),
                stdout=stdout,
            )

            self.assertEqual(result, 0)
            self.assertEqual(stdout.getvalue().count("Updated Superpowers for project:"), 2)
            joined = "\n".join(" ".join(call.argv) for call in calls)
            self.assertIn("projects/alpha/codex-home", joined)
            self.assertIn("projects/beta/codex-home", joined)
            self.assertEqual(joined.count("marketplace add obra/superpowers"), 2)
            self.assertEqual(
                joined.count(
                    "marketplace add anthropics/claude-plugins-official"
                ),
                2,
            )
            self.assertEqual(
                joined.count(
                    "plugin install superpowers@claude-plugins-official"
                ),
                2,
            )

    def test_project_update_profile_preserves_custom_rules_without_runner(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            project_dir = root / "projects/agent-container"
            codex_home = project_dir / "codex-home"
            for directory in (root, root / "projects", project_dir):
                directory.mkdir(exist_ok=True, mode=0o700)
                directory.chmod(0o700)
            seed_codex_home(Path(__file__).resolve().parents[2] / "profiles/codex", codex_home)
            codex_home.chmod(0o700)
            rules_file = codex_home / "rules/default.rules"
            rules_file.write_text("custom-rule\n", encoding="utf-8")
            ProjectRecord(
                Repository.parse("jj1xgo/agent-container"),
                Path(temp) / "handovers",
            ).write(project_dir / "project.json")
            calls = []
            stdout = StringIO()

            result = main(
                ["project", "update-profile", "agent-container"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=lambda spec: calls.append(spec),
                stdout=stdout,
            )

            self.assertEqual(result, 0)
            self.assertEqual(calls, [])
            self.assertIn("custom-rule\n", rules_file.read_text(encoding="utf-8"))
            self.assertIn("agent-handover", rules_file.read_text(encoding="utf-8"))
            self.assertIn("Updated managed handover profile", stdout.getvalue())

    def _authenticated_state(self, root: Path) -> None:
        hosts = root / "gh/hosts.yml"
        hosts.parent.mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        hosts.write_text("github.com:\n", encoding="utf-8")
        hosts.chmod(0o600)

    def _broker_app_state(self, root: Path) -> None:
        root.mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        broker = root / "github-broker"
        broker.mkdir(mode=0o700)
        app = broker / "app.json"
        key = broker / "private-key.pem"
        app.write_text(
            json.dumps(
                {
                    "client_id": "Iv1abcdefghijk",
                    "installation_id": 123,
                    "repository_id": 456,
                }
            ),
            encoding="utf-8",
        )
        key.write_text("private-key-marker", encoding="utf-8")
        app.chmod(0o600)
        key.chmod(0o600)

    def test_project_add_clones_through_broker_without_gh_state(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            handovers = Path(temp) / "handovers"
            (handovers / "agent-container").mkdir(parents=True)
            self._broker_app_state(root)
            broker = BrokerRuntimeMount(
                root / "github-broker/run/agent-container/session",
                Repository.parse("jj1xgo/agent-container"),
            )
            calls = []

            def runner(spec):
                calls.append(spec)
                if "clone" in spec.argv:
                    (root / "workspaces/agent-container/.git").mkdir(parents=True)
                return successful_podman_result(spec)

            with patch(
                "agent_container.agentctl.UploadPackBrokerRuntime.create"
            ) as create:
                context = create.return_value
                context.__enter__.return_value = broker
                result = main(
                    [
                        "project", "add", "jj1xgo/agent-container",
                        "--handover-root", str(handovers),
                        "--github-broker", "--protected-branch", "main",
                        "--protected-branch", "master",
                        "--confirm-force-push-ruleset",
                    ],
                    environment={"AGENT_CONTAINER_HOME": str(root)},
                    runner=runner,
                    git_remote_reader=lambda path: (
                        "https://github.com/jj1xgo/agent-container.git"
                    ),
                )

            self.assertEqual(result, 0)
            clone = " ".join(next(call.argv for call in calls if "clone" in call.argv))
            self.assertIn("git clone https://github.com/jj1xgo/agent-container.git", clone)
            self.assertIn("dst=/run/agent-broker,ro=true", clone)
            self.assertNotIn("gh auth git-credential", clone)
            self.assertFalse((root / "gh").exists())
            policy = json.loads(
                (root / "projects/agent-container/github-broker.json").read_text()
            )
            self.assertEqual(policy["protected_branches"], ["main", "master"])
            create.assert_called_once()
            context.__exit__.assert_called_once()

    def test_project_add_broker_start_failure_never_clones_or_falls_back(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            handovers = Path(temp) / "handovers"
            (handovers / "agent-container").mkdir(parents=True)
            self._broker_app_state(root)
            calls = []

            with patch(
                "agent_container.agentctl.UploadPackBrokerRuntime.create",
                side_effect=OSError("private-key-marker"),
            ):
                result = main(
                    [
                        "project", "add", "jj1xgo/agent-container",
                        "--handover-root", str(handovers),
                        "--github-broker", "--confirm-force-push-ruleset",
                    ],
                    environment={"AGENT_CONTAINER_HOME": str(root)},
                    runner=lambda spec: calls.append(spec)
                    or successful_podman_result(spec),
                    stderr=StringIO(),
                )

            self.assertEqual(result, 1)
            self.assertFalse((root / "workspaces/agent-container").exists())
            self.assertFalse((root / "gh").exists())
            self.assertFalse(any("clone" in call.argv for call in calls))
            self.assertFalse(
                (root / "projects/agent-container/project.json").exists()
            )

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
            self.assertTrue((root / "projects/agent-container/claude-config").is_dir())
            self.assertEqual(len(calls), 8)
            joined_calls = [" ".join(call.argv) for call in calls]
            self.assertTrue(any("obra/superpowers --ref main" in call for call in joined_calls))
            self.assertTrue(any("superpowers@superpowers-dev" in call for call in joined_calls))
            self.assertTrue(any("superpowers@claude-plugins-official" in call for call in joined_calls))
            for forbidden in ("checkout", "reset", "clean", "fetch"):
                self.assertFalse(any(forbidden in call.argv for call in calls))

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
            self.assertEqual(len(calls), 7)


class AgentCtlRunDoctorTest(unittest.TestCase):
    CODEX_DOCTOR = [
        "podman-version",
        "podman-rootless",
        "image",
        "base-image-id",
        "project-image",
        "agent-node",
        "project-node",
        "codex-version",
        "private-state",
        "codex-auth",
        "gh-hosts",
        "project-metadata",
        "workspace-origin",
        "handover-project",
        "network-policy",
    ]
    CLAUDE_DOCTOR = [
        "podman-version",
        "podman-rootless",
        "image",
        "base-image-id",
        "project-image",
        "agent-node",
        "project-node",
        "claude-managed-policy",
        "claude-version",
        "private-state",
        "claude-auth",
        "claude-auth-status",
        "claude-config",
        "claude-project-credentials",
        "gh-hosts",
        "project-metadata",
        "workspace-origin",
        "handover-project",
        "network-policy",
    ]
    ALL_DOCTOR = [
        "podman-version",
        "podman-rootless",
        "image",
        "base-image-id",
        "project-image",
        "agent-node",
        "project-node",
        "claude-managed-policy",
        "codex-version",
        "claude-version",
        "private-state",
        "codex-auth",
        "claude-auth",
        "claude-auth-status",
        "claude-config",
        "claude-project-credentials",
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
            root / "shared-auth/claude",
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
        claude_token_file = root / "shared-auth/claude/oauth-token"
        claude_token_file.write_text("x" * 32, encoding="ascii")
        claude_token_file.chmod(0o600)
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
        agent: str = "codex",
        environment_root: Path | None = None,
        remote_url: str = "https://github.com/jj1xgo/agent-container.git",
    ) -> None:
        calls = []
        stdout = StringIO()
        stderr = StringIO()

        result = main(
            ["run", "agent-container", "--agent", agent],
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
        self._assert_private_values_absent(
            rendered, "DO-NOT-PRINT-CREDENTIAL-BODY"
        )

    def _is_claude_status_spec(self, spec) -> bool:
        return spec.argv[-8:] == (
            "python3",
            "-m",
            "agent_container.claude_launcher",
            "/run/secrets/claude-oauth-token",
            "--",
            "claude",
            "auth",
            "status",
        )

    def _successful_doctor_runner(self, spec):
        if spec.argv == ("podman", "info", "--format", "{{.Host.Security.Rootless}}"):
            return subprocess.CompletedProcess(spec.argv, 0, stdout="true\n")
        if spec.argv[:3] == ("podman", "image", "inspect"):
            return subprocess.CompletedProcess(spec.argv, 0, stdout="sha256:base\n")
        if spec.argv[-2:] == ("/opt/agent-node/bin/node", "--version"):
            return subprocess.CompletedProcess(spec.argv, 0, stdout="v24.7.0\n")
        return subprocess.CompletedProcess(spec.argv, 0, stdout="podman version 5.8\n")

    def _doctor_check_names(self, rendered: str) -> list[str]:
        return [line.split()[1].removesuffix(":") for line in rendered.splitlines()]

    def _assert_private_values_absent(
        self, rendered: str, *private_values: str
    ) -> None:
        self.assertTrue(all(value not in rendered for value in private_values))

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
            self.assertEqual(
                calls[-1].argv[-5:-2],
                ("localhost/agent-container:dev", "codex", "--approve-for-me"),
            )
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

    def test_run_builds_and_uses_missing_project_image(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            config_dir = root / "workspaces/agent-container/.agent-container.d"
            config_dir.mkdir()
            (config_dir / "packages.txt").write_text("make\n", encoding="utf-8")
            calls = []
            builder_calls = []
            output = StringIO()
            expected_key = project_image_key(
                "sha256:base", ProjectImageConfig(("make",), None), "amd64"
            )
            expected_image = project_image_name("agent-container", expected_key)

            def runner(spec):
                calls.append(spec)
                if spec.argv[:5] == (
                    "podman", "image", "inspect", "--format", "{{.Id}}"
                ):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="sha256:base\n")
                if spec.argv == ("podman", "info", "--format", "{{.Host.Arch}}"):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="amd64\n")
                if spec.argv[:3] == ("podman", "image", "exists") and spec.argv[-1] == expected_image:
                    return subprocess.CompletedProcess(spec.argv, 1)
                return successful_podman_result(spec)

            def runtime_builder(*args):
                builder_calls.append(args)
                return run_codex_spec(*args)

            result = main(
                ["run", "agent-container"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=runner,
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                runtime_spec_builder=runtime_builder,
                stdout=output,
            )

            self.assertEqual(result, 0)
            self.assertEqual(builder_calls[0][2], expected_image)
            self.assertEqual(sum(call.argv[:2] == ("podman", "build") for call in calls), 1)
            self.assertIn("project image missing; building", output.getvalue())

    def test_run_github_broker_starts_context_before_broker_only_spec(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            (root / "gh/hosts.yml").unlink()
            (root / "gh").rmdir()
            repository = Repository.parse("jj1xgo/agent-container")
            broker = BrokerRuntimeMount(root / "github-broker/run/session", repository)
            builder_calls = []
            calls = []

            def builder(*args):
                builder_calls.append(args)
                return run_codex_spec(*args)

            with patch(
                "agent_container.agentctl.UploadPackBrokerRuntime.create"
            ) as create:
                context = create.return_value
                context.__enter__.return_value = broker
                context.__exit__.return_value = None
                result = main(
                    ["run", "agent-container", "--github-broker"],
                    environment={"AGENT_CONTAINER_HOME": str(root)},
                    runner=lambda spec: calls.append(spec)
                    or successful_podman_result(spec),
                    git_remote_reader=lambda path: (
                        "https://github.com/jj1xgo/agent-container.git"
                    ),
                    runtime_spec_builder=builder,
                    stdout=StringIO(),
                )

            self.assertEqual(result, 0)
            self.assertEqual(len(builder_calls), 1)
            self.assertIs(builder_calls[0][-1], broker)
            create.assert_called_once()
            context.__enter__.assert_called_once()
            context.__exit__.assert_called_once()
            runtime = " ".join(calls[-1].argv)
            self.assertIn("dst=/run/agent-broker,ro=true", runtime)
            self.assertNotIn("src=" + str(root / "gh"), runtime)
            self.assertNotIn("gh auth git-credential", runtime)

    def test_run_enters_only_selected_brokers_and_passes_independent_mounts(self) -> None:
        combinations = (
            ("codex", False),
            ("codex", True),
            ("claude", False),
            ("claude", True),
        )
        for agent, github_enabled in combinations:
            with self.subTest(agent=agent, github_enabled=github_enabled):
                with TemporaryDirectory() as temp:
                    root, handover_project = self._runtime_state(temp)
                    events = []
                    github_mount = BrokerRuntimeMount(
                        root / "github-broker/run/session",
                        Repository.parse("jj1xgo/agent-container"),
                    )
                    handover_mount = HandoverRuntimeMount(
                        root / "handover-broker/run/session"
                    )
                    github_context = _TrackingBrokerContext(
                        "github", github_mount, events
                    )
                    handover_context = _TrackingBrokerContext(
                        "handover",
                        handover_mount,
                        events,
                        on_enter=lambda: self.assertTrue(
                            (root / "projects/agent-container/claude-config").is_dir()
                        ),
                    )
                    builder_calls = []

                    def runner(spec):
                        if spec.argv == ("runtime", agent):
                            events.append("runtime")
                        elif spec.argv[-3:] == (
                            "python3",
                            "-m",
                            "agent_container.claude_policy",
                        ):
                            events.append("policy")
                        return successful_podman_result(spec)

                    def builder(*args):
                        events.append("builder")
                        builder_calls.append(args)
                        self.assertEqual(github_context.active, github_enabled)
                        self.assertEqual(handover_context.active, agent == "claude")
                        return CommandSpec(("runtime", agent), {})

                    original_read = agentctl._read_runtime_project

                    def tracked_read(path):
                        events.append("metadata")
                        return original_read(path)

                    argv = ["run", "agent-container", "--agent", agent]
                    if github_enabled:
                        argv.append("--github-broker")
                    with (
                        patch(
                            "agent_container.agentctl._read_runtime_project",
                            side_effect=tracked_read,
                        ) as read_project,
                        patch(
                            "agent_container.agentctl.UploadPackBrokerRuntime.create",
                            return_value=github_context,
                        ) as create_github,
                        patch(
                            "agent_container.agentctl.HandoverBrokerRuntime.create",
                            return_value=handover_context,
                        ) as create_handover,
                    ):
                        result = main(
                            argv,
                            environment={"AGENT_CONTAINER_HOME": str(root)},
                            runner=runner,
                            git_remote_reader=lambda path: (
                                "https://github.com/jj1xgo/agent-container.git"
                            ),
                            runtime_spec_builders={agent: builder},
                            stdout=StringIO(),
                        )

                    self.assertEqual(result, 0)
                    self.assertEqual(read_project.call_count, 1)
                    self.assertEqual(len(builder_calls), 1)
                    args = builder_calls[0]
                    self.assertEqual(args[:5], (
                        agentctl.StateLayout(root, "agent-container"),
                        handover_project,
                        "localhost/agent-container:dev",
                        os.getuid(),
                        os.getgid(),
                    ))
                    if agent == "claude":
                        self.assertIs(args[5], handover_mount)
                        create_handover.assert_called_once()
                    else:
                        self.assertEqual(len(args), 6 if github_enabled else 5)
                        create_handover.assert_not_called()
                    if github_enabled:
                        self.assertIs(args[-1], github_mount)
                        create_github.assert_called_once()
                    else:
                        create_github.assert_not_called()
                    expected = ["metadata"]
                    if agent == "claude":
                        expected.append("policy")
                    if github_enabled:
                        expected.append("github-enter")
                    if agent == "claude":
                        expected.append("handover-enter")
                    expected.extend(("builder", "runtime"))
                    if agent == "claude":
                        expected.append("handover-exit")
                    if github_enabled:
                        expected.append("github-exit")
                    self.assertEqual(events, expected)

    def test_run_handover_broker_enter_failure_never_builds_or_runs_runtime(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            events = []
            github_mount = BrokerRuntimeMount(
                root / "github-broker/run/session",
                Repository.parse("jj1xgo/agent-container"),
            )
            github_context = _TrackingBrokerContext(
                "github", github_mount, events
            )
            failed_handover = _TrackingBrokerContext(
                "handover",
                HandoverRuntimeMount(root / "handover-broker/run/session"),
                events,
                enter_error=HandoverBrokerRuntimeError(
                    "DO-NOT-PRINT-HANDOVER-RUNTIME-DETAIL"
                ),
            )
            runtime_specs = []
            builder_calls = []
            stderr = StringIO()

            def runner(spec):
                if spec.argv and spec.argv[0] == "runtime":
                    runtime_specs.append(spec)
                return successful_podman_result(spec)

            def builder(*args):
                builder_calls.append(args)
                return CommandSpec(("runtime", "claude"), {})

            with (
                patch(
                    "agent_container.agentctl.UploadPackBrokerRuntime.create",
                    return_value=github_context,
                ),
                patch(
                    "agent_container.agentctl.HandoverBrokerRuntime.create",
                    return_value=failed_handover,
                ),
            ):
                result = main(
                    [
                        "run",
                        "agent-container",
                        "--agent",
                        "claude",
                        "--github-broker",
                    ],
                    environment={"AGENT_CONTAINER_HOME": str(root)},
                    runner=runner,
                    git_remote_reader=lambda path: (
                        "https://github.com/jj1xgo/agent-container.git"
                    ),
                    runtime_spec_builders={"claude": builder},
                    stdout=StringIO(),
                    stderr=stderr,
                )

            self.assertEqual(result, 1)
            self.assertEqual(builder_calls, [])
            self.assertEqual(runtime_specs, [])
            self.assertEqual(
                events, ["github-enter", "handover-enter", "github-exit"]
            )
            self.assertNotIn(
                "DO-NOT-PRINT-HANDOVER-RUNTIME-DETAIL", stderr.getvalue()
            )

    def test_run_rejects_handover_overlap_before_any_broker_or_podman(self) -> None:
        for direction in ("same", "ancestor", "descendant"):
            with self.subTest(direction=direction), TemporaryDirectory() as temp:
                root, _ = self._runtime_state(temp)
                workspace = root / "workspaces/agent-container"
                if direction == "same":
                    handover_root = workspace.parent
                elif direction == "ancestor":
                    handover_root = root / "projects"
                else:
                    handover_root = workspace / "handover-root"
                    (handover_root / "agent-container").mkdir(parents=True)
                project_file = root / "projects/agent-container/project.json"
                project_file.unlink()
                ProjectRecord(
                    Repository.parse("jj1xgo/agent-container"),
                    handover_root.resolve(),
                ).write(project_file)
                podman_calls = []
                stderr = StringIO()
                github_context = _TrackingBrokerContext(
                    "github",
                    BrokerRuntimeMount(
                        root / "github-broker/run/session",
                        Repository.parse("jj1xgo/agent-container"),
                    ),
                    [],
                )
                handover_context = _TrackingBrokerContext(
                    "handover",
                    HandoverRuntimeMount(root / "handover-broker/run/session"),
                    [],
                )

                with (
                    patch(
                        "agent_container.agentctl.UploadPackBrokerRuntime.create",
                        return_value=github_context,
                    ) as create_github,
                    patch(
                        "agent_container.agentctl.HandoverBrokerRuntime.create",
                        return_value=handover_context,
                    ) as create_handover,
                ):
                    result = main(
                        [
                            "run",
                            "agent-container",
                            "--agent",
                            "claude",
                            "--github-broker",
                        ],
                        environment={"AGENT_CONTAINER_HOME": str(root)},
                        runner=lambda spec: podman_calls.append(spec)
                        or successful_podman_result(spec),
                        git_remote_reader=lambda path: (
                            "https://github.com/jj1xgo/agent-container.git"
                        ),
                        stdout=StringIO(),
                        stderr=stderr,
                    )

                self.assertEqual(result, 1)
                self.assertIn("overlap", stderr.getvalue())
                self.assertEqual(podman_calls, [])
                create_github.assert_not_called()
                create_handover.assert_not_called()

    def test_run_nonzero_cleans_up_handover_and_github_brokers(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            events = []
            github_context = _TrackingBrokerContext(
                "github",
                BrokerRuntimeMount(
                    root / "github-broker/run/session",
                    Repository.parse("jj1xgo/agent-container"),
                ),
                events,
            )
            handover_context = _TrackingBrokerContext(
                "handover",
                HandoverRuntimeMount(root / "handover-broker/run/session"),
                events,
            )

            def runner(spec):
                if spec.argv == ("runtime", "claude"):
                    events.append("runtime-nonzero")
                    return subprocess.CompletedProcess(spec.argv, 23)
                return successful_podman_result(spec)

            with (
                patch(
                    "agent_container.agentctl.UploadPackBrokerRuntime.create",
                    return_value=github_context,
                ),
                patch(
                    "agent_container.agentctl.HandoverBrokerRuntime.create",
                    return_value=handover_context,
                ),
            ):
                result = main(
                    [
                        "run",
                        "agent-container",
                        "--agent",
                        "claude",
                        "--github-broker",
                    ],
                    environment={"AGENT_CONTAINER_HOME": str(root)},
                    runner=runner,
                    git_remote_reader=lambda path: (
                        "https://github.com/jj1xgo/agent-container.git"
                    ),
                    runtime_spec_builders={
                        "claude": lambda *args: CommandSpec(("runtime", "claude"), {})
                    },
                    stdout=StringIO(),
                    stderr=StringIO(),
                )

            self.assertEqual(result, 23)
            self.assertEqual(
                events,
                [
                    "github-enter",
                    "handover-enter",
                    "runtime-nonzero",
                    "handover-exit",
                    "github-exit",
                ],
            )

    def test_run_uses_current_project_image_without_build(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            config_dir = root / "workspaces/agent-container/.agent-container.d"
            config_dir.mkdir()
            (config_dir / "packages.txt").write_text("make\n", encoding="utf-8")
            calls = []
            builder_calls = []

            def runner(spec):
                calls.append(spec)
                if spec.argv[:3] == ("podman", "image", "inspect"):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="sha256:base\n")
                if spec.argv == ("podman", "info", "--format", "{{.Host.Arch}}"):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="amd64\n")
                return successful_podman_result(spec)

            result = main(
                ["run", "agent-container"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=runner,
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                runtime_spec_builder=lambda *args: builder_calls.append(args) or run_codex_spec(*args),
            )

            self.assertEqual(result, 0)
            self.assertEqual(sum(call.argv[:2] == ("podman", "build") for call in calls), 0)
            self.assertIn("localhost/agent-container-project:", builder_calls[0][2])

    def test_run_project_image_build_failure_never_starts_runtime(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            config_dir = root / "workspaces/agent-container/.agent-container.d"
            config_dir.mkdir()
            (config_dir / "packages.txt").write_text("make\n", encoding="utf-8")
            builder_calls = []

            def runner(spec):
                if spec.argv[:3] == ("podman", "image", "inspect"):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="sha256:base\n")
                if spec.argv == ("podman", "info", "--format", "{{.Host.Arch}}"):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="amd64\n")
                if spec.argv[:3] == ("podman", "image", "exists") and "project:" in spec.argv[-1]:
                    return subprocess.CompletedProcess(spec.argv, 1)
                if spec.argv[:2] == ("podman", "build"):
                    return subprocess.CompletedProcess(spec.argv, 17)
                return successful_podman_result(spec)

            result = main(
                ["run", "agent-container"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=runner,
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                runtime_spec_builder=lambda *args: builder_calls.append(args) or run_codex_spec(*args),
            )

            self.assertEqual(result, 17)
            self.assertEqual(builder_calls, [])

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
            self._assert_private_values_absent(
                stderr.getvalue(), "DO-NOT-PRINT-CREDENTIAL-BODY"
            )

    def test_run_claude_creates_project_config_after_image_preflight(self) -> None:
        with TemporaryDirectory() as temp:
            root, handover_project = self._runtime_state(temp)
            calls = []
            builder_calls = []
            output = StringIO()
            config = root / "projects/agent-container/claude-config"

            def runtime_spec_builder(*args):
                builder_calls.append(args)
                self.assertEqual(len(calls), 4)
                self.assertEqual(calls[-2].argv[:3], ("podman", "image", "exists"))
                self.assertEqual(
                    calls[-1].argv[-3:],
                    ("python3", "-m", "agent_container.claude_policy"),
                )
                self.assertTrue(config.is_dir())
                self.assertEqual(config.stat().st_mode & 0o777, 0o700)
                return run_claude_spec(*args)

            broker_events = []
            broker_context = _TrackingBrokerContext(
                "handover",
                HandoverRuntimeMount(root / "handover-broker/run/session"),
                broker_events,
            )
            with patch(
                "agent_container.agentctl.HandoverBrokerRuntime.create",
                return_value=broker_context,
            ):
                result = main(
                    ["run", "agent-container", "--agent", "claude"],
                    environment={"AGENT_CONTAINER_HOME": str(root)},
                    runner=lambda spec: calls.append(spec)
                    or successful_podman_result(spec),
                    git_remote_reader=lambda path: (
                        "https://github.com/jj1xgo/agent-container.git"
                    ),
                    runtime_spec_builders={"claude": runtime_spec_builder},
                    stdout=output,
                )

            self.assertEqual(result, 0)
            self.assertEqual(len(builder_calls), 1)
            self.assertEqual(len(calls), 5)
            self.assertEqual(broker_events, ["handover-enter", "handover-exit"])
            self.assertEqual(calls[-1].argv[-1], "claude")
            self.assertIn(
                f"src={handover_project},dst=/handovers/agent-container",
                " ".join(calls[-1].argv),
            )
            self.assertEqual(
                output.getvalue(), "Starting Claude for project: agent-container\n"
            )

    def test_run_claude_rejects_invalid_managed_policy_before_state_or_runtime(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            calls = []
            builder_calls = []
            stdout = StringIO()
            stderr = StringIO()
            config = root / "projects/agent-container/claude-config"

            def runner(spec):
                calls.append(spec)
                if spec.argv[-3:] == (
                    "python3", "-m", "agent_container.claude_policy"
                ):
                    return subprocess.CompletedProcess(
                        spec.argv,
                        9,
                        stdout="DO-NOT-PRINT-POLICY-OUTPUT",
                        stderr="DO-NOT-PRINT-POLICY-ERROR",
                    )
                return successful_podman_result(spec)

            result = main(
                ["run", "agent-container", "--agent", "claude"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=runner,
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                runtime_spec_builders={
                    "claude": lambda *args: builder_calls.append(args)
                    or run_claude_spec(*args)
                },
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(result, 9)
            self.assertEqual(builder_calls, [])
            self.assertFalse(config.exists())
            self.assertNotIn("Starting Claude", stdout.getvalue())
            self.assertNotIn("DO-NOT-PRINT-POLICY", stdout.getvalue())
            self.assertNotIn("DO-NOT-PRINT-POLICY", stderr.getvalue())
            self.assertEqual(
                sum(
                    call.argv[-3:]
                    == ("python3", "-m", "agent_container.claude_policy")
                    for call in calls
                ),
                1,
            )

    def test_run_claude_missing_image_does_not_create_config(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            calls = []
            config = root / "projects/agent-container/claude-config"

            def runner(spec):
                calls.append(spec)
                if spec.argv[:3] == ("podman", "image", "exists"):
                    return subprocess.CompletedProcess(spec.argv, 29)
                return successful_podman_result(spec)

            result = main(
                ["run", "agent-container", "--agent", "claude"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=runner,
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                stderr=StringIO(),
            )

            self.assertEqual(result, 29)
            self.assertEqual(len(calls), 3)
            self.assertFalse(config.exists())

    def test_run_claude_rejects_legacy_project_credentials_before_podman(self) -> None:
        marker = "DO-NOT-PRINT-CLAUDE-PROJECT-CREDENTIAL"
        cases = ("empty", "non-empty", "symlink")
        for entry_kind in cases:
            with self.subTest(entry=entry_kind), TemporaryDirectory() as temp:
                root, _ = self._runtime_state(temp)
                config = root / "projects/agent-container/claude-config"
                config.mkdir(mode=0o700)
                credentials = config / ".credentials.json"
                if entry_kind == "symlink":
                    target = Path(temp) / marker
                    target.write_text(marker, encoding="utf-8")
                    credentials.symlink_to(target)
                else:
                    credentials.write_text(
                        "" if entry_kind == "empty" else marker,
                        encoding="utf-8",
                    )
                    credentials.chmod(0o600)
                calls = []
                stderr = StringIO()

                result = main(
                    ["run", "agent-container", "--agent", "claude"],
                    environment={"AGENT_CONTAINER_HOME": str(root)},
                    runner=lambda spec: calls.append(spec)
                    or successful_podman_result(spec),
                    git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                    stderr=stderr,
                )

                self.assertEqual(result, 1)
                self.assertEqual(calls, [])
                rendered = stderr.getvalue()
                self.assertIn("unsupported legacy Claude project credential", rendered)
                self.assertEqual(rendered.find(marker), -1)

    def test_run_claude_rejects_invalid_active_token_before_podman(self) -> None:
        marker = "DO-NOT-PRINT-INVALID-CLAUDE-TOKEN"
        cases = ("format", "mode", "symlink")
        for failure in cases:
            with self.subTest(failure=failure), TemporaryDirectory() as temp:
                root, _ = self._runtime_state(temp)
                legacy_auth = root / "shared-auth/claude/.credentials.json"
                legacy_auth.write_text("{}", encoding="ascii")
                legacy_auth.chmod(0o600)
                token = root / "shared-auth/claude/oauth-token"
                if failure == "format":
                    token.write_text("invalid", encoding="ascii")
                elif failure == "mode":
                    token.chmod(0o644)
                else:
                    target = Path(temp) / marker
                    target.write_text("x" * 32, encoding="ascii")
                    target.chmod(0o600)
                    token.unlink()
                    token.symlink_to(target)
                calls = []
                stderr = StringIO()

                result = main(
                    ["run", "agent-container", "--agent", "claude"],
                    environment={"AGENT_CONTAINER_HOME": str(root)},
                    runner=lambda spec: calls.append(spec)
                    or successful_podman_result(spec),
                    git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                    stderr=stderr,
                )

                self.assertEqual(result, 1)
                self.assertEqual(calls, [])
                self.assertEqual(stderr.getvalue().find(marker), -1)

    def test_run_claude_hides_state_root_for_token_or_config_failures(self) -> None:
        marker = "DO-NOT-PRINT-CLAUDE-RUNTIME-STATE-ROOT"
        cases = (
            "token-missing",
            "token-format",
            "token-mode",
            "token-symlink",
            "config-file",
            "config-mode",
            "config-symlink",
        )
        for failure in cases:
            with self.subTest(failure=failure), TemporaryDirectory() as temp:
                state_parent = Path(temp) / marker
                state_parent.mkdir()
                root, _ = self._runtime_state(str(state_parent))
                token = root / "shared-auth/claude/oauth-token"
                config = root / "projects/agent-container/claude-config"
                if failure == "token-missing":
                    token.unlink()
                elif failure == "token-format":
                    token.write_text("invalid", encoding="ascii")
                elif failure == "token-mode":
                    token.chmod(0o644)
                elif failure == "token-symlink":
                    target = Path(temp) / "token-target"
                    target.write_text("x" * 32, encoding="ascii")
                    target.chmod(0o600)
                    token.unlink()
                    token.symlink_to(target)
                elif failure == "config-file":
                    config.write_text("not a directory", encoding="ascii")
                elif failure == "config-mode":
                    config.mkdir(mode=0o700)
                    config.chmod(0o755)
                else:
                    target = Path(temp) / "config-target"
                    target.mkdir(mode=0o700)
                    config.symlink_to(target, target_is_directory=True)
                calls = []
                stderr = StringIO()

                result = main(
                    ["run", "agent-container", "--agent", "claude"],
                    environment={"AGENT_CONTAINER_HOME": str(root)},
                    runner=lambda spec: calls.append(spec)
                    or successful_podman_result(spec),
                    git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                    stderr=stderr,
                )

                rendered = stderr.getvalue()
                self.assertEqual(result, 1)
                self.assertEqual(calls, [])
                self.assertEqual(
                    rendered.startswith(
                        "error: Claude runtime state validation failed"
                    ),
                    True,
                )
                self.assertEqual(rendered.find(marker), -1)

    def test_run_claude_rejects_broad_auth_or_config_without_secret_output(self) -> None:
        cases = (
            ("auth", "shared-auth/claude/oauth-token", 0o644),
            ("config", "projects/agent-container/claude-config", 0o755),
        )
        for name, relative_path, mode in cases:
            with self.subTest(boundary=name), TemporaryDirectory() as temp:
                root, _ = self._runtime_state(temp)
                if name == "auth":
                    legacy_auth = root / "shared-auth/claude/.credentials.json"
                    legacy_auth.write_text("{}", encoding="ascii")
                    legacy_auth.chmod(0o600)
                path = root / relative_path
                if name == "config":
                    path.mkdir(mode=0o700)
                path.chmod(mode)
                calls = []
                stderr = StringIO()

                result = main(
                    ["run", "agent-container", "--agent", "claude"],
                    environment={"AGENT_CONTAINER_HOME": str(root)},
                    runner=lambda spec: calls.append(spec)
                    or successful_podman_result(spec),
                    git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                    stderr=stderr,
                )

                self.assertEqual(result, 1)
                self.assertEqual(calls, [])
                self.assertEqual(
                    stderr.getvalue().startswith(
                        "error: Claude runtime state validation failed"
                    ),
                    True,
                )
                self._assert_private_values_absent(
                    stderr.getvalue(), "DO-NOT-PRINT-CLAUDE-CREDENTIAL"
                )

    def test_run_claude_rejects_symlinked_auth_or_config_without_secret_output(self) -> None:
        cases = (
            ("auth", "shared-auth/claude/oauth-token", "auth-target"),
            ("config", "projects/agent-container/claude-config", "config-target"),
        )
        for name, relative_path, target_name in cases:
            with self.subTest(boundary=name), TemporaryDirectory() as temp:
                root, _ = self._runtime_state(temp)
                if name == "auth":
                    legacy_auth = root / "shared-auth/claude/.credentials.json"
                    legacy_auth.write_text("{}", encoding="ascii")
                    legacy_auth.chmod(0o600)
                path = root / relative_path
                target = Path(temp) / target_name
                if name == "auth":
                    path.unlink()
                    target.write_text(
                        "DO-NOT-PRINT-CLAUDE-CREDENTIAL", encoding="utf-8"
                    )
                    target.chmod(0o600)
                else:
                    target.mkdir(mode=0o700)
                path.symlink_to(target, target_is_directory=name == "config")
                calls = []
                stderr = StringIO()

                result = main(
                    ["run", "agent-container", "--agent", "claude"],
                    environment={"AGENT_CONTAINER_HOME": str(root)},
                    runner=lambda spec: calls.append(spec)
                    or successful_podman_result(spec),
                    git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                    stderr=stderr,
                )

                self.assertEqual(result, 1)
                self.assertEqual(calls, [])
                self.assertEqual(
                    stderr.getvalue().startswith(
                        "error: Claude runtime state validation failed"
                    ),
                    True,
                )
                self._assert_private_values_absent(
                    stderr.getvalue(), "DO-NOT-PRINT-CLAUDE-CREDENTIAL"
                )

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
                if spec.argv[:3] == ("podman", "image", "inspect"):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="sha256:base\n")
                if spec.argv[-2:] == ("/opt/agent-node/bin/node", "--version"):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="v24.7.0\n")
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
            self.assertIn("PASS  base-image-id: sha256:base", rendered)
            self.assertIn("PASS  agent-node: v24.7.0", rendered)
            self.assertIn("PASS  project-node: unconfigured", rendered)
            self.assertIn("PASS  codex-auth: present, mode 0600", rendered)
            self.assertIn("PASS  gh-hosts: present, mode 0600", rendered)
            self.assertIn("PASS  workspace-origin: exact HTTPS origin", rendered)
            self.assertIn(
                "WARN  network-policy: outbound network is not domain-restricted in Phase 2",
                rendered,
            )
            self._assert_private_values_absent(
                rendered, "DO-NOT-PRINT-CREDENTIAL-BODY"
            )
            self.assertEqual(
                [call.argv for call in calls],
                [
                    ("podman", "--version"),
                    ("podman", "info", "--format", "{{.Host.Security.Rootless}}"),
                    ("podman", "image", "exists", "localhost/agent-container:dev"),
                    (
                        "podman", "image", "inspect", "--format", "{{.Id}}",
                        "localhost/agent-container:dev",
                    ),
                    (
                        "podman", "run", "--rm", "--read-only", "--cap-drop=all",
                        "--security-opt=no-new-privileges",
                        "--userns=keep-id:uid=1000,gid=1000",
                        "--tmpfs=/tmp:rw,nosuid,nodev,size=512m",
                        "localhost/agent-container:dev", "/opt/agent-node/bin/node", "--version",
                    ),
                    (
                        "podman", "run", "--rm", "--read-only", "--cap-drop=all",
                        "--security-opt=no-new-privileges",
                        "--userns=keep-id:uid=1000,gid=1000",
                        "--tmpfs=/tmp:rw,nosuid,nodev,size=512m",
                        "localhost/agent-container:dev", "codex", "--version",
                    ),
                ],
            )
            self.assertEqual(self._doctor_check_names(rendered), self.CODEX_DOCTOR)

    def test_doctor_github_broker_validates_local_state_without_gh_credentials(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            (root / "gh/hosts.yml").unlink()
            (root / "gh").rmdir()
            broker_root = root / "github-broker"
            broker_root.mkdir(mode=0o700)
            app = broker_root / "app.json"
            key = broker_root / "private-key.pem"
            policy = root / "projects/agent-container/github-broker.json"
            app.write_text(
                json.dumps(
                    {
                        "client_id": "Iv1abcdefghijk",
                        "installation_id": 123,
                        "repository_id": 456,
                    }
                ),
                encoding="utf-8",
            )
            key.write_text("private-key-marker", encoding="utf-8")
            policy.write_text(
                json.dumps(
                    {
                        "repository": "jj1xgo/agent-container",
                        "default_branch": "main",
                        "protected_branches": ["main", "master"],
                        "ruleset_confirmed": True,
                    }
                ),
                encoding="utf-8",
            )
            for path in (app, key, policy):
                path.chmod(0o600)
            output = StringIO()

            result = main(
                ["doctor", "agent-container", "--github-broker"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=self._successful_doctor_runner,
                git_remote_reader=lambda path: (
                    "https://github.com/jj1xgo/agent-container.git"
                ),
                stdout=output,
            )

            rendered = output.getvalue()
            self.assertEqual(result, 0)
            self.assertIn(
                "PASS  github-broker: local App and project policy valid", rendered
            )
            self.assertNotIn("gh-hosts", rendered)
            self.assertNotIn("private-key-marker", rendered)
            self.assertNotIn(str(root), rendered)

    def test_doctor_claude_reports_authenticated_launcher_status(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            (root / "projects/agent-container/claude-config").mkdir(mode=0o700)
            calls = []
            output = StringIO()

            def doctor_runner(spec):
                calls.append(spec)
                if spec.argv == ("podman", "info", "--format", "{{.Host.Security.Rootless}}"):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="true\n")
                if spec.argv[:3] == ("podman", "image", "inspect"):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="sha256:base\n")
                if spec.argv[-2:] == ("/opt/agent-node/bin/node", "--version"):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="v24.7.0\n")
                return subprocess.CompletedProcess(spec.argv, 0, stdout="ignored\n")

            result = main(
                ["doctor", "agent-container", "--agent", "claude"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=doctor_runner,
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                stdout=output,
            )

            self.assertEqual(result, 0)
            rendered = output.getvalue()
            self.assertIn("PASS  claude-auth: present, mode 0600", rendered)
            self.assertIn("PASS  claude-auth-status: authenticated", rendered)
            status_calls = [spec for spec in calls if self._is_claude_status_spec(spec)]
            self.assertEqual(len(status_calls), 1)
            token_mounts = [
                argument
                for argument in status_calls[0].argv
                if argument.startswith("type=bind,")
                and "dst=/run/secrets/claude-oauth-token" in argument
            ]
            self.assertEqual(len(token_mounts), 1)
            mount_fields = dict(
                field.split("=", 1) for field in token_mounts[0].split(",")
            )
            self.assertTrue(
                hmac.compare_digest(
                    mount_fields.get("src", ""),
                    str(root / "shared-auth/claude/oauth-token"),
                )
            )
            self.assertEqual(
                mount_fields.get("dst"), "/run/secrets/claude-oauth-token"
            )
            self.assertEqual(mount_fields.get("ro"), "true")

    def test_doctor_claude_status_failure_is_generic_and_secret_safe(self) -> None:
        marker = "DO-NOT-PRINT-CLAUDE-STATUS-OUTPUT"
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            (root / "projects/agent-container/claude-config").mkdir(mode=0o700)
            output = StringIO()

            def doctor_runner(spec):
                if spec.argv == ("podman", "info", "--format", "{{.Host.Security.Rootless}}"):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="true\n")
                if spec.argv[:3] == ("podman", "image", "inspect"):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="sha256:base\n")
                if spec.argv[-2:] == ("/opt/agent-node/bin/node", "--version"):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="v24.7.0\n")
                if self._is_claude_status_spec(spec):
                    return subprocess.CompletedProcess(
                        spec.argv, 17, stdout=marker, stderr=marker
                    )
                return subprocess.CompletedProcess(spec.argv, 0, stdout="ignored\n")

            result = main(
                ["doctor", "agent-container", "--agent", "claude"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=doctor_runner,
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                stdout=output,
            )

            rendered = output.getvalue()
            self.assertEqual(result, 1)
            self.assertIn("PASS  claude-auth: present, mode 0600", rendered)
            self.assertIn("FAIL  claude-auth-status: command failed", rendered)
            self.assertEqual(
                [line for line in rendered.splitlines() if line.startswith("FAIL")],
                ["FAIL  claude-auth-status: command failed"],
            )
            self.assertEqual(rendered.find(marker), -1)

    def test_doctor_claude_skips_status_when_token_is_invalid(self) -> None:
        cases = ("missing", "format", "mode")
        for failure in cases:
            with self.subTest(failure=failure), TemporaryDirectory() as temp:
                root, _ = self._runtime_state(temp)
                (root / "projects/agent-container/claude-config").mkdir(mode=0o700)
                token = root / "shared-auth/claude/oauth-token"
                if failure == "missing":
                    token.unlink()
                elif failure == "format":
                    token.write_text("invalid", encoding="ascii")
                else:
                    token.chmod(0o644)
                calls = []
                output = StringIO()

                result = main(
                    ["doctor", "agent-container", "--agent", "claude"],
                    environment={"AGENT_CONTAINER_HOME": str(root)},
                    runner=lambda spec: calls.append(spec)
                    or self._successful_doctor_runner(spec),
                    git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                    stdout=output,
                )

                rendered = output.getvalue()
                self.assertEqual(result, 1)
                self.assertIn("FAIL  claude-auth:", rendered)
                self.assertIn(
                    "FAIL  claude-auth-status: not run: token invalid", rendered
                )
                self.assertFalse(any(self._is_claude_status_spec(spec) for spec in calls))

    def test_doctor_claude_skips_status_when_image_is_missing(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            (root / "projects/agent-container/claude-config").mkdir(mode=0o700)
            calls = []
            output = StringIO()

            def doctor_runner(spec):
                calls.append(spec)
                if spec.argv == ("podman", "info", "--format", "{{.Host.Security.Rootless}}"):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="true\n")
                if spec.argv[:3] == ("podman", "image", "exists"):
                    return subprocess.CompletedProcess(spec.argv, 1)
                return subprocess.CompletedProcess(spec.argv, 0)

            result = main(
                ["doctor", "agent-container", "--agent", "claude"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=doctor_runner,
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                stdout=output,
            )

            self.assertEqual(result, 1)
            self.assertIn(
                "FAIL  claude-auth-status: not run: image unavailable",
                output.getvalue(),
            )
            self.assertFalse(any(self._is_claude_status_spec(spec) for spec in calls))

    def test_doctor_claude_reports_legacy_project_credentials_without_body(self) -> None:
        marker = "DO-NOT-PRINT-CLAUDE-PROJECT-CREDENTIAL"
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            config = root / "projects/agent-container/claude-config"
            config.mkdir(mode=0o700)
            credentials = config / ".credentials.json"
            credentials.write_text(marker, encoding="utf-8")
            credentials.chmod(0o600)
            output = StringIO()

            result = main(
                ["doctor", "agent-container", "--agent", "claude"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=self._successful_doctor_runner,
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                stdout=output,
            )

            rendered = output.getvalue()
            self.assertEqual(result, 1)
            self.assertIn("FAIL  claude-project-credentials:", rendered)
            self.assertEqual(rendered.find(marker), -1)

    def test_doctor_reports_checks_in_selected_agent_order(self) -> None:
        cases = (
            ("codex", self.CODEX_DOCTOR),
            ("claude", self.CLAUDE_DOCTOR),
            ("all", self.ALL_DOCTOR),
        )
        for agent, expected_order in cases:
            with self.subTest(agent=agent), TemporaryDirectory() as temp:
                root, _ = self._runtime_state(temp)
                config = root / "projects/agent-container/claude-config"
                config.mkdir(mode=0o700)
                output = StringIO()

                result = main(
                    ["doctor", "agent-container", "--agent", agent],
                    environment={"AGENT_CONTAINER_HOME": str(root)},
                    runner=self._successful_doctor_runner,
                    git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                    stdout=output,
                )

                self.assertEqual(result, 0)
                self.assertEqual(
                    self._doctor_check_names(output.getvalue()), expected_order
                )

    def test_doctor_all_runs_common_probes_once_and_reports_missing_claude_state(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            claude_auth = root / "shared-auth/claude/oauth-token"
            claude_auth.unlink()
            output = StringIO()
            calls = []

            def doctor_runner(spec):
                calls.append(spec)
                if spec.argv == ("podman", "info", "--format", "{{.Host.Security.Rootless}}"):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="true\n")
                if spec.argv[:3] == ("podman", "image", "inspect"):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="sha256:base\n")
                if spec.argv[-2:] == ("/opt/agent-node/bin/node", "--version"):
                    return subprocess.CompletedProcess(spec.argv, 0, stdout="v24.7.0\n")
                if spec.argv[-2:] in (("codex", "--version"), ("claude", "--version")):
                    return subprocess.CompletedProcess(
                        spec.argv,
                        0,
                        stdout="version\n",
                        stderr="DO-NOT-PRINT-PROBE-STDERR",
                    )
                return subprocess.CompletedProcess(spec.argv, 0, stdout="podman version 5.8\n")

            result = main(
                ["doctor", "agent-container", "--agent", "all"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=doctor_runner,
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                stdout=output,
            )

            rendered = output.getvalue()
            self.assertEqual(result, 1)
            self.assertEqual(
                [call.argv[:3] for call in calls].count(("podman", "--version")), 1
            )
            self.assertEqual(
                [call.argv[:2] for call in calls].count(("podman", "info")), 1
            )
            self.assertEqual(
                [call.argv[:3] for call in calls].count(("podman", "image", "exists")),
                1,
            )
            self.assertEqual(
                [
                    call.argv[-2:]
                    for call in calls
                    if call.argv[:2] == ("podman", "run")
                    and call.argv[-2] in ("codex", "claude")
                ],
                [("codex", "--version"), ("claude", "--version")],
            )
            self.assertIn("FAIL  claude-auth:", rendered)
            self.assertIn("FAIL  claude-config:", rendered)
            self.assertEqual(self._doctor_check_names(rendered), self.ALL_DOCTOR)
            self.assertFalse(
                (root / "projects/agent-container/claude-config").exists()
            )
            self._assert_private_values_absent(
                rendered,
                "DO-NOT-PRINT-CLAUDE-CREDENTIAL",
                "DO-NOT-PRINT-PROBE-STDERR",
            )

    def test_doctor_does_not_print_state_root_in_claude_validation_failures(self) -> None:
        marker = "DO-NOT-PRINT-STATE-ROOT"
        cases = (
            ("claude-auth", "shared-auth/claude/oauth-token"),
            ("claude-config", "projects/agent-container/claude-config"),
        )
        for failed_check, relative_path in cases:
            with self.subTest(check=failed_check), TemporaryDirectory() as temp:
                state_parent = Path(temp) / marker
                state_parent.mkdir()
                root, _ = self._runtime_state(str(state_parent))
                config = root / "projects/agent-container/claude-config"
                config.mkdir(mode=0o700)
                if failed_check == "claude-auth":
                    (root / relative_path).unlink()
                else:
                    config.rmdir()
                output = StringIO()

                result = main(
                    ["doctor", "agent-container", "--agent", "claude"],
                    environment={"AGENT_CONTAINER_HOME": str(root)},
                    runner=self._successful_doctor_runner,
                    git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                    stdout=output,
                )

                rendered = output.getvalue()
                self.assertEqual(result, 1)
                self.assertIn(f"FAIL  {failed_check}:", rendered)
                self.assertEqual(rendered.find(marker), -1)

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
            self._assert_private_values_absent(
                output.getvalue(), "DO-NOT-PRINT-CREDENTIAL-BODY"
            )

    def test_doctor_reports_project_image_states_without_building(self) -> None:
        for state, exists_code, prior in (
            ("current", 0, ""),
            ("stale", 1, "localhost/agent-container-project:agent-container-old"),
            ("missing", 1, ""),
        ):
            with self.subTest(state=state), TemporaryDirectory() as temp:
                root, _ = self._runtime_state(temp)
                config_dir = root / "workspaces/agent-container/.agent-container.d"
                config_dir.mkdir()
                (config_dir / "packages.txt").write_text("make\n", encoding="utf-8")
                (config_dir / "node-version.txt").write_text(
                    "22.23.1\n", encoding="utf-8"
                )
                calls = []
                output = StringIO()

                def runner(spec):
                    calls.append(spec)
                    if spec.argv[:3] == ("podman", "image", "inspect"):
                        return subprocess.CompletedProcess(spec.argv, 0, stdout="sha256:base\n")
                    if spec.argv == ("podman", "info", "--format", "{{.Host.Arch}}"):
                        return subprocess.CompletedProcess(spec.argv, 0, stdout="amd64\n")
                    if spec.argv[:3] == ("podman", "image", "exists") and "project:" in spec.argv[-1]:
                        return subprocess.CompletedProcess(spec.argv, exists_code)
                    if spec.argv[:2] == ("podman", "images"):
                        return subprocess.CompletedProcess(spec.argv, 0, stdout=prior)
                    if spec.argv[-2:] == ("/opt/project-node/bin/node", "--version"):
                        return subprocess.CompletedProcess(
                            spec.argv, 0, stdout="v22.23.1\n"
                        )
                    return self._successful_doctor_runner(spec)

                result = main(
                    ["doctor", "agent-container"],
                    environment={"AGENT_CONTAINER_HOME": str(root)},
                    runner=runner,
                    git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                    stdout=output,
                )

                self.assertIn(f"project-image: {state}", output.getvalue())
                if state == "current":
                    self.assertIn(
                        "PASS  project-node: v22.23.1", output.getvalue()
                    )
                else:
                    self.assertIn(
                        "FAIL  project-node: image unavailable", output.getvalue()
                    )
                self.assertEqual(result, 0 if state == "current" else 1)
                self.assertFalse(any(call.argv[:2] == ("podman", "build") for call in calls))

    def test_doctor_rejects_invalid_project_image_config_without_content(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            config_dir = root / "workspaces/agent-container/.agent-container.d"
            config_dir.mkdir()
            secret = "make;DO-NOT-PRINT-PROJECT-CONFIG"
            (config_dir / "packages.txt").write_text(secret, encoding="utf-8")
            calls = []
            output = StringIO()

            result = main(
                ["doctor", "agent-container"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=lambda spec: calls.append(spec) or self._successful_doctor_runner(spec),
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                stdout=output,
            )

            self.assertEqual(result, 1)
            self.assertIn("FAIL  project-image: state validation failed", output.getvalue())
            self.assertNotIn(secret, output.getvalue())
            self.assertFalse(any(call.argv[:2] == ("podman", "build") for call in calls))

    def test_doctor_reports_claude_managed_policy_without_command_output(self) -> None:
        for returncode, level in ((0, "PASS"), (9, "FAIL")):
            with self.subTest(returncode=returncode), TemporaryDirectory() as temp:
                root, _ = self._runtime_state(temp)
                (root / "projects/agent-container/claude-config").mkdir(mode=0o700)
                calls = []
                output = StringIO()

                def runner(spec):
                    calls.append(spec)
                    if spec.argv[-3:] == (
                        "python3", "-m", "agent_container.claude_policy"
                    ):
                        return subprocess.CompletedProcess(
                            spec.argv,
                            returncode,
                            stdout="DO-NOT-PRINT-POLICY-OUTPUT",
                            stderr="DO-NOT-PRINT-POLICY-ERROR",
                        )
                    return self._successful_doctor_runner(spec)

                main(
                    ["doctor", "agent-container", "--agent", "claude"],
                    environment={"AGENT_CONTAINER_HOME": str(root)},
                    runner=runner,
                    git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
                    stdout=output,
                )

                self.assertIn(
                    f"{level}  claude-managed-policy: "
                    f"{'valid' if returncode == 0 else 'invalid'}",
                    output.getvalue(),
                )
                self.assertNotIn("DO-NOT-PRINT-POLICY", output.getvalue())
                self.assertEqual(
                    sum(
                        call.argv[-3:]
                        == ("python3", "-m", "agent_container.claude_policy")
                        for call in calls
                    ),
                    1,
                )

    def test_codex_doctor_does_not_probe_claude_policy(self) -> None:
        with TemporaryDirectory() as temp:
            root, _ = self._runtime_state(temp)
            calls = []

            main(
                ["doctor", "agent-container", "--agent", "codex"],
                environment={"AGENT_CONTAINER_HOME": str(root)},
                runner=lambda spec: calls.append(spec) or self._successful_doctor_runner(spec),
                git_remote_reader=lambda path: "https://github.com/jj1xgo/agent-container.git",
            )

            self.assertFalse(
                any(
                    call.argv[-3:]
                    == ("python3", "-m", "agent_container.claude_policy")
                    for call in calls
                )
            )

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
                    self.CODEX_DOCTOR,
                )
                self._assert_private_values_absent(
                    rendered, "DO-NOT-PRINT-CREDENTIAL-BODY"
                )

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
            self._assert_private_values_absent(
                stderr.getvalue(), "DO-NOT-PRINT-CREDENTIAL-BODY"
            )

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


class AgentCtlMigrationTest(unittest.TestCase):
    def _migration_state(self, temp: str) -> tuple[Path, Path]:
        base = Path(temp).resolve()
        root = base / "state"
        project_dir = root / "projects/demo"
        project_dir.mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        (root / "projects").chmod(0o700)
        project_dir.chmod(0o700)
        handovers = base / "handovers"
        handovers.mkdir()
        ProjectRecord(
            Repository("safe", "demo"), handovers.resolve()
        ).write(project_dir / "project.json")

        source = base / "source"
        selected = source / "plugins/cache/local-marketplace/issue-ops/1.2.3"
        unselected = source / "plugins/cache/local-marketplace/unselected/9.9.9"
        selected.mkdir(parents=True)
        unselected.mkdir(parents=True)
        (source / "CLAUDE.md").write_text("safe instructions\n", encoding="utf-8")
        (selected / "plugin.json").write_text(
            '{"name": "issue-ops"}\n', encoding="utf-8"
        )
        (unselected / "plugin.json").write_text(
            "DO-NOT-PRINT-CREDENTIAL-BODY", encoding="utf-8"
        )
        (source / "plugins/installed_plugins.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "plugins": {
                        "issue-ops@local-marketplace": [
                            {
                                "scope": "user",
                                "installPath": str(selected),
                                "version": "1.2.3",
                            }
                        ],
                        "unselected@local-marketplace": [
                            {
                                "scope": "user",
                                "installPath": str(unselected),
                                "version": "9.9.9",
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        (source / "plugins/known_marketplaces.json").write_text(
            json.dumps(
                {
                    "local-marketplace": {
                        "source": {"source": "github", "repo": "safe/plugins"}
                    }
                }
            ),
            encoding="utf-8",
        )
        return root, source

    def _run_migration(
        self,
        root: Path,
        source: Path,
        *extra: str,
    ) -> tuple[int, StringIO, StringIO, list]:
        stdout = StringIO()
        stderr = StringIO()
        calls = []
        result = main(
            [
                "migrate",
                "claude",
                "demo",
                "--from",
                str(source),
                "--plugin",
                "issue-ops@local-marketplace",
                *extra,
            ],
            environment={"AGENT_CONTAINER_HOME": str(root)},
            runner=lambda spec: calls.append(spec),
            stdout=stdout,
            stderr=stderr,
        )
        return result, stdout, stderr, calls

    def test_migrate_dry_run_prints_safe_plan_without_creating_destination(self) -> None:
        with TemporaryDirectory() as temp:
            root, source = self._migration_state(temp)

            result, stdout, stderr, calls = self._run_migration(root, source)

            destination = root / "projects/demo/claude-config"
            self.assertEqual(result, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(calls, [])
            self.assertFalse(destination.exists())
            output = stdout.getvalue()
            self.assertIn("COPY file CLAUDE.md\n", output)
            self.assertIn(
                "COPY file plugins/cache/local-marketplace/issue-ops/1.2.3/plugin.json\n",
                output,
            )
            self.assertIn("COPY file plugins/installed_plugins.json\n", output)
            self.assertIn("COPY file plugins/known_marketplaces.json\n", output)
            self.assertIn(f"DESTINATION {destination}\n", output)
            self.assertTrue(output.endswith("MODE dry-run\n"))
            self.assertNotIn("unselected", output)
            self.assertNotIn("DO-NOT-PRINT-CREDENTIAL-BODY", output)

    def test_migrate_apply_publishes_only_selected_state_without_runner(self) -> None:
        with TemporaryDirectory() as temp:
            root, source = self._migration_state(temp)

            result, stdout, stderr, calls = self._run_migration(
                root, source, "--apply"
            )

            destination = root / "projects/demo/claude-config"
            self.assertEqual(result, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(calls, [])
            self.assertTrue((destination / "CLAUDE.md").is_file())
            self.assertTrue(
                (
                    destination
                    / "plugins/cache/local-marketplace/issue-ops/1.2.3/plugin.json"
                ).is_file()
            )
            self.assertFalse(
                (destination / "plugins/cache/local-marketplace/unselected").exists()
            )
            installed = json.loads(
                (destination / "plugins/installed_plugins.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                set(installed["plugins"]), {"issue-ops@local-marketplace"}
            )
            self.assertTrue(stdout.getvalue().endswith("MODE apply\n"))

    def test_migrate_rejects_invalid_identifiers_before_filesystem_access(self) -> None:
        cases = (
            ("../invalid-project", "issue-ops@local-marketplace", "project_id"),
            ("demo", "invalid-plugin", "plugin"),
        )
        for project, plugin, expected_error in cases:
            with (
                self.subTest(project=project, plugin=plugin),
                TemporaryDirectory() as temp,
            ):
                root = Path(temp) / "must-not-be-created"
                calls = []
                stderr = StringIO()

                result = main(
                    [
                        "migrate",
                        "claude",
                        project,
                        "--from",
                        "/DO-NOT-PRINT-CREDENTIAL-BODY",
                        "--plugin",
                        plugin,
                    ],
                    environment={"AGENT_CONTAINER_HOME": str(root)},
                    runner=lambda spec: calls.append(spec),
                    stderr=stderr,
                )

                self.assertEqual(result, 1)
                self.assertEqual(calls, [])
                self.assertFalse(root.exists())
                self.assertIn(expected_error, stderr.getvalue())
                self.assertNotIn(
                    "DO-NOT-PRINT-CREDENTIAL-BODY", stderr.getvalue()
                )

    def test_migrate_validates_all_inputs_before_destination_mutation(self) -> None:
        cases = ("missing-project-metadata", "missing-source", "existing-destination")
        for case in cases:
            with self.subTest(case=case), TemporaryDirectory() as temp:
                root, source = self._migration_state(temp)
                destination = root / "projects/demo/claude-config"
                if case == "missing-project-metadata":
                    (root / "projects/demo/project.json").unlink()
                elif case == "missing-source":
                    shutil.rmtree(source)
                else:
                    destination.mkdir(mode=0o700)
                    (destination / "DO-NOT-PRINT-CREDENTIAL-BODY").write_text(
                        "unchanged", encoding="utf-8"
                    )

                result, stdout, stderr, calls = self._run_migration(
                    root, source, "--apply"
                )

                self.assertEqual(result, 1)
                self.assertEqual(calls, [])
                self.assertEqual(stdout.getvalue(), "")
                self.assertNotIn("DO-NOT-PRINT-CREDENTIAL-BODY", stderr.getvalue())
                if case != "existing-destination":
                    self.assertFalse(destination.exists())
                else:
                    self.assertEqual(
                        (destination / "DO-NOT-PRINT-CREDENTIAL-BODY").read_text(
                            encoding="utf-8"
                        ),
                        "unchanged",
                    )

    def test_migrate_metadata_error_hides_secret_values_and_never_runs_podman(self) -> None:
        with TemporaryDirectory() as temp:
            root, source = self._migration_state(temp)
            (source / "plugins/known_marketplaces.json").write_text(
                '{"local-marketplace": "DO-NOT-PRINT-CREDENTIAL-BODY"}',
                encoding="utf-8",
            )

            result, stdout, stderr, calls = self._run_migration(
                root, source, "--apply"
            )

            self.assertEqual(result, 1)
            self.assertEqual(calls, [])
            self.assertEqual(stdout.getvalue(), "")
            self.assertNotIn("DO-NOT-PRINT-CREDENTIAL-BODY", stderr.getvalue())
            self.assertFalse((root / "projects/demo/claude-config").exists())


class AgentCtlParserTest(unittest.TestCase):
    def test_version_reports_project_version(self) -> None:
        stdout = StringIO()

        with patch("sys.stdout", stdout), self.assertRaises(SystemExit) as raised:
            parser().parse_args(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertRegex(
            stdout.getvalue(),
            r"^agentctl (?:0\.2\.0|0\.3\.0-dev\.\d+"
            r"(?:\+g[0-9a-f]{7}(?:\.dirty)?)?)\n$",
        )

    def test_new_command_contract(self) -> None:
        build = parser().parse_args(["build"])
        self.assertEqual(
            (
                getattr(build, "node_version", None),
                build.codex_version,
                build.claude_version,
            ),
            ("latest", "latest", "latest"),
        )
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
