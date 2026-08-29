import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

import agent_container.github_broker_runtime as github_broker_runtime
from agent_container.github_app import GitHubAppMetadata
from agent_container.github_broker_runtime import UploadPackBrokerRuntime
from agent_container.github_broker_runtime import broker_token_metadata
from agent_container.github_broker_runtime import load_broker_policy
from agent_container.github_broker_runtime import upgrade_legacy_broker_policy
from agent_container.github_broker_runtime import write_broker_policy
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.state import ProjectRecord
from agent_container.state import Repository
from agent_container.state import StateLayout


class BrokerRuntimePolicyTest(unittest.TestCase):
    def test_loads_legacy_global_policy_with_true_ruleset_marker(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "policy.json"
            path.write_text(
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
            path.chmod(0o600)
            record = ProjectRecord(
                Repository.parse("jj1xgo/agent-container"), Path("/handovers")
            )

            policy = load_broker_policy(path, record, "agent-container")

            self.assertEqual(policy.repository, record.repository)
            self.assertEqual(policy.default_branch, "main")
            self.assertIsNone(policy.repository_id)

    def test_loads_legacy_bound_policy_with_true_ruleset_marker(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "policy.json"
            path.write_text(
                json.dumps(
                    {
                        "repository": "jj1xgo/agent-container",
                        "repository_id": 123,
                        "default_branch": "main",
                        "protected_branches": ["main"],
                        "ruleset_confirmed": True,
                    }
                ),
                encoding="utf-8",
            )
            path.chmod(0o600)
            record = ProjectRecord(
                Repository.parse("jj1xgo/agent-container"), Path("/handovers")
            )

            policy = load_broker_policy(path, record, "agent-container")

            self.assertEqual(policy.repository_id, 123)

    def test_loads_new_bound_policy_without_ruleset_marker(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "policy.json"
            path.write_text(
                json.dumps(
                    {
                        "repository": "jj1xgo/agent-container",
                        "repository_id": 123,
                        "default_branch": "main",
                        "protected_branches": ["main"],
                    }
                ),
                encoding="utf-8",
            )
            path.chmod(0o600)
            record = ProjectRecord(
                Repository.parse("jj1xgo/agent-container"), Path("/handovers")
            )

            policy = load_broker_policy(path, record, "agent-container")

            self.assertEqual(policy.repository_id, 123)

    def test_rejects_invalid_bound_ids_and_unknown_policy_schemas(self) -> None:
        record = ProjectRecord(
            Repository.parse("jj1xgo/agent-container"), Path("/handovers")
        )
        legacy = {
            "repository": "jj1xgo/agent-container",
            "default_branch": "main",
            "protected_branches": ["main"],
            "ruleset_confirmed": True,
        }
        with TemporaryDirectory() as temp:
            path = Path(temp) / "policy.json"
            for repository_id in (True, 0, -1, "123"):
                with self.subTest(repository_id=repository_id):
                    path.write_text(
                        json.dumps(legacy | {"repository_id": repository_id}),
                        encoding="utf-8",
                    )
                    path.chmod(0o600)
                    with self.assertRaises(ValueError):
                        load_broker_policy(path, record, "agent-container")

            invalid_schemas = (
                legacy | {"ruleset_confirmed": False},
                legacy | {"unexpected": True},
                {
                    "repository": "jj1xgo/agent-container",
                    "repository_id": 123,
                    "default_branch": "main",
                    "protected_branches": ["main"],
                    "unexpected": True,
                },
            )
            for payload in invalid_schemas:
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    path.chmod(0o600)
                    with self.assertRaises(ValueError):
                        load_broker_policy(path, record, "agent-container")

    def test_writes_only_new_bound_four_key_policy_schema(self) -> None:
        legacy = BrokerPolicy.create(
            project_id="agent-container",
            repository="jj1xgo/agent-container",
            default_branch="main",
            protected_branches=("main",),
        )
        bound = BrokerPolicy.create(
            project_id="agent-container",
            repository="jj1xgo/agent-container",
            repository_id=123,
            default_branch="main",
            protected_branches=("main",),
            require_repository_id=True,
        )
        with TemporaryDirectory() as temp:
            legacy_path = Path(temp) / "legacy.json"
            with self.assertRaises(ValueError):
                write_broker_policy(legacy_path, legacy)
            self.assertFalse(legacy_path.exists())

            path = Path(temp) / "policy.json"
            write_broker_policy(path, bound)

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {
                    "repository": "jj1xgo/agent-container",
                    "repository_id": 123,
                    "default_branch": "main",
                    "protected_branches": ["main"],
                },
            )

    def test_rejects_mismatched_policy_repository(self) -> None:
        record = ProjectRecord(
            Repository.parse("jj1xgo/agent-container"), Path("/handovers")
        )
        with TemporaryDirectory() as temp:
            path = Path(temp) / "policy.json"
            path.write_text(
                json.dumps(
                    {
                        "repository": "jj1xgo/other",
                        "default_branch": "main",
                        "protected_branches": ["main"],
                        "ruleset_confirmed": True,
                    }
                ),
                encoding="utf-8",
            )
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                load_broker_policy(path, record, "agent-container")


class BrokerPolicyUpgradeTest(unittest.TestCase):
    def _policy(
        self,
        *,
        repository: str = "jj1xgo/agent-container-smoke",
        repository_id: int | None = None,
        default_branch: str = "main",
        protected_branches: tuple[str, ...] = ("main",),
    ) -> BrokerPolicy:
        return BrokerPolicy.create(
            project_id="agent-container-smoke",
            repository=repository,
            repository_id=repository_id,
            default_branch=default_branch,
            protected_branches=protected_branches,
            require_repository_id=repository_id is not None,
        )

    def _write_policy(self, path: Path, policy: BrokerPolicy) -> None:
        payload = {
            "repository": policy.repository.slug,
            "default_branch": policy.default_branch,
            "protected_branches": sorted(policy.protected_branches),
            "ruleset_confirmed": True,
        }
        if policy.repository_id is not None:
            payload["repository_id"] = policy.repository_id
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)

    def test_upgrades_only_policy_and_preserves_private_mode(self) -> None:
        existing = self._policy()
        requested = self._policy(repository_id=123)
        record = ProjectRecord(existing.repository, Path("/handovers"))
        with TemporaryDirectory() as temp:
            path = Path(temp) / "github-broker.json"
            sibling = path.parent / "smoke-fixtures.json"
            self._write_policy(path, existing)
            sibling.write_bytes(b'{"fixture":"credential-free"}\n')
            original_sibling = sibling.read_bytes()

            upgrade_legacy_broker_policy(path, existing, requested)

            upgraded = load_broker_policy(
                path, record, "agent-container-smoke"
            )
            self.assertEqual(upgraded.repository_id, 123)
            self.assertEqual(sibling.read_bytes(), original_sibling)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(path.parent.glob(".github-broker.json.*")), [])

    def test_rejects_non_id_policy_mismatches_without_changing_bytes(self) -> None:
        cases = (
            self._policy(
                repository="jj1xgo/other",
                repository_id=123,
            ),
            self._policy(
                repository_id=123,
                default_branch="master",
                protected_branches=("master",),
            ),
            self._policy(
                repository_id=123,
                protected_branches=("main", "master"),
            ),
        )
        for requested in cases:
            with self.subTest(requested=requested), TemporaryDirectory() as temp:
                path = Path(temp) / "github-broker.json"
                existing = self._policy()
                self._write_policy(path, existing)
                original = path.read_bytes()

                with self.assertRaises(ValueError):
                    upgrade_legacy_broker_policy(path, existing, requested)

                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(path.parent.glob(".github-broker.json.*")), [])

    def test_rejects_already_bound_policy_without_changing_bytes(self) -> None:
        for requested_id in (123, 456):
            with self.subTest(requested_id=requested_id), TemporaryDirectory() as temp:
                path = Path(temp) / "github-broker.json"
                existing = self._policy(repository_id=123)
                requested = self._policy(repository_id=requested_id)
                self._write_policy(path, existing)
                original = path.read_bytes()

                with self.assertRaises(ValueError):
                    upgrade_legacy_broker_policy(path, existing, requested)

                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(path.parent.glob(".github-broker.json.*")), [])

    def test_rejects_symlink_target_without_changing_target_bytes(self) -> None:
        existing = self._policy()
        requested = self._policy(repository_id=123)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target.json"
            path = root / "github-broker.json"
            self._write_policy(target, existing)
            original = target.read_bytes()
            path.symlink_to(target)

            with self.assertRaises(ValueError):
                upgrade_legacy_broker_policy(path, existing, requested)

            self.assertTrue(path.is_symlink())
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(".github-broker.json.*")), [])

    def test_rejects_wrong_mode_without_changing_bytes(self) -> None:
        existing = self._policy()
        requested = self._policy(repository_id=123)
        with TemporaryDirectory() as temp:
            path = Path(temp) / "github-broker.json"
            self._write_policy(path, existing)
            path.chmod(0o644)
            original = path.read_bytes()

            with self.assertRaises(PermissionError):
                upgrade_legacy_broker_policy(path, existing, requested)

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
            self.assertEqual(list(path.parent.glob(".github-broker.json.*")), [])

    def test_rejects_non_private_parent_without_changing_bytes(self) -> None:
        existing = self._policy()
        requested = self._policy(repository_id=123)
        with TemporaryDirectory() as temp:
            path = Path(temp) / "github-broker.json"
            self._write_policy(path, existing)
            path.parent.chmod(0o755)
            original = path.read_bytes()

            with self.assertRaises(PermissionError):
                upgrade_legacy_broker_policy(path, existing, requested)

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o755)
            self.assertEqual(list(path.parent.glob(".github-broker.json.*")), [])

    def test_rejects_wrong_parent_owner_without_changing_bytes(self) -> None:
        existing = self._policy()
        requested = self._policy(repository_id=123)
        with TemporaryDirectory() as temp:
            path = Path(temp) / "github-broker.json"
            self._write_policy(path, existing)
            original = path.read_bytes()
            real_fstat = os.fstat

            def wrong_directory_owner(descriptor):
                result = real_fstat(descriptor)
                if not stat.S_ISDIR(result.st_mode):
                    return result
                values = list(result)
                values[4] = os.getuid() + 1
                return os.stat_result(values)

            self.assertEqual(path.stat().st_uid, os.getuid())
            with mock.patch.object(
                github_broker_runtime.os,
                "fstat",
                side_effect=wrong_directory_owner,
            ):
                with self.assertRaisesRegex(
                    PermissionError,
                    "GitHub broker policy parent is not private",
                ):
                    upgrade_legacy_broker_policy(path, existing, requested)

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(".github-broker.json.*")), [])

    def test_rejects_wrong_policy_file_owner_without_changing_bytes(self) -> None:
        existing = self._policy()
        requested = self._policy(repository_id=123)
        with TemporaryDirectory() as temp:
            path = Path(temp) / "github-broker.json"
            self._write_policy(path, existing)
            original = path.read_bytes()
            real_fstat = os.fstat

            def wrong_regular_file_owner(descriptor):
                result = real_fstat(descriptor)
                if not stat.S_ISREG(result.st_mode):
                    return result
                values = list(result)
                values[4] = os.getuid() + 1
                return os.stat_result(values)

            with mock.patch.object(
                github_broker_runtime.os,
                "fstat",
                side_effect=wrong_regular_file_owner,
            ):
                with self.assertRaises(PermissionError):
                    upgrade_legacy_broker_policy(path, existing, requested)

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(".github-broker.json.*")), [])

    def test_write_failure_cleans_temp_and_preserves_original_bytes(self) -> None:
        existing = self._policy()
        requested = self._policy(repository_id=123)
        with TemporaryDirectory() as temp:
            path = Path(temp) / "github-broker.json"
            self._write_policy(path, existing)
            original = path.read_bytes()

            with mock.patch.object(
                github_broker_runtime,
                "_write_all",
                side_effect=OSError("injected write failure"),
            ):
                with self.assertRaises(OSError):
                    upgrade_legacy_broker_policy(path, existing, requested)

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(".github-broker.json.*")), [])

    def test_file_fsync_failure_cleans_temp_and_preserves_original_bytes(self) -> None:
        existing = self._policy()
        requested = self._policy(repository_id=123)
        with TemporaryDirectory() as temp:
            path = Path(temp) / "github-broker.json"
            self._write_policy(path, existing)
            original = path.read_bytes()

            with mock.patch.object(
                github_broker_runtime.os,
                "fsync",
                side_effect=OSError("injected fsync failure"),
            ):
                with self.assertRaises(OSError):
                    upgrade_legacy_broker_policy(path, existing, requested)

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(".github-broker.json.*")), [])

    def test_replace_failure_cleans_temp_and_preserves_original_bytes(self) -> None:
        existing = self._policy()
        requested = self._policy(repository_id=123)
        with TemporaryDirectory() as temp:
            path = Path(temp) / "github-broker.json"
            self._write_policy(path, existing)
            original = path.read_bytes()

            with mock.patch.object(
                github_broker_runtime.os,
                "replace",
                side_effect=OSError("injected replace failure"),
            ):
                with self.assertRaises(OSError):
                    upgrade_legacy_broker_policy(path, existing, requested)

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(".github-broker.json.*")), [])

    def test_rejects_replaced_parent_and_cleans_held_directory_temp(self) -> None:
        existing = self._policy()
        requested = self._policy(repository_id=123)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            parent = root / "project"
            parent.mkdir(mode=0o700)
            path = parent / "github-broker.json"
            self._write_policy(path, existing)
            original = path.read_bytes()
            moved_parent = root / "project-before-swap"
            real_validate = github_broker_runtime._validate_policy_parent_identity
            validations = 0

            def swap_before_second_validation(directory, expected):
                nonlocal validations
                validations += 1
                if validations == 2:
                    directory.rename(moved_parent)
                    directory.mkdir(mode=0o700)
                    self._write_policy(directory / path.name, existing)
                return real_validate(directory, expected)

            with mock.patch.object(
                github_broker_runtime,
                "_validate_policy_parent_identity",
                side_effect=swap_before_second_validation,
            ):
                with self.assertRaises(ValueError):
                    upgrade_legacy_broker_policy(path, existing, requested)

            self.assertEqual((moved_parent / path.name).read_bytes(), original)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(
                list(moved_parent.glob(".github-broker.json.*")),
                [],
            )
            self.assertEqual(list(parent.glob(".github-broker.json.*")), [])


class BrokerRuntimeConstructionTest(unittest.TestCase):
    def test_composes_bound_repository_id_without_mutating_app_metadata(self) -> None:
        app = GitHubAppMetadata(
            client_id="Iv1abcdefghijk",
            installation_id=11,
            repository_id=22,
            private_key=Path("/private-key.pem"),
        )
        bound = BrokerPolicy.create(
            project_id="smoke",
            repository="jj1xgo/agent-container-smoke",
            repository_id=33,
            default_branch="main",
            protected_branches=("main",),
        )

        composed = broker_token_metadata(app, bound)

        self.assertEqual(composed.repository_id, 33)
        self.assertEqual(app.repository_id, 22)

    def test_composes_legacy_policy_with_global_repository_id(self) -> None:
        app = GitHubAppMetadata(
            client_id="Iv1abcdefghijk",
            installation_id=11,
            repository_id=22,
            private_key=Path("/private-key.pem"),
        )
        legacy = BrokerPolicy.create(
            project_id="production",
            repository="jj1xgo/agent-container",
            default_branch="main",
            protected_branches=("main",),
        )

        composed = broker_token_metadata(app, legacy)

        self.assertEqual(composed.repository_id, 22)

    def test_composes_two_projects_with_separate_repository_ids(self) -> None:
        app = GitHubAppMetadata(
            client_id="Iv1abcdefghijk",
            installation_id=11,
            repository_id=22,
            private_key=Path("/private-key.pem"),
        )
        policies = (
            BrokerPolicy.create(
                project_id="smoke",
                repository="jj1xgo/agent-container-smoke",
                repository_id=33,
                default_branch="main",
                protected_branches=("main",),
            ),
            BrokerPolicy.create(
                project_id="staging",
                repository="jj1xgo/agent-container-staging",
                repository_id=44,
                default_branch="main",
                protected_branches=("main",),
            ),
        )

        first, second = (broker_token_metadata(app, policy) for policy in policies)

        self.assertEqual(first.repository_id, 33)
        self.assertEqual(second.repository_id, 44)
        for metadata in (first, second):
            self.assertEqual(metadata.client_id, app.client_id)
            self.assertEqual(metadata.installation_id, app.installation_id)
            self.assertEqual(metadata.private_key, app.private_key)

    @mock.patch("agent_container.github_broker_runtime.GitHubIssueTransport")
    @mock.patch("agent_container.github_broker_runtime.GitHubPullRequestTransport")
    @mock.patch("agent_container.github_broker_runtime.GitHubReceivePackTransport")
    @mock.patch("agent_container.github_broker_runtime.GitHubUploadPackTransport")
    @mock.patch("agent_container.github_broker_runtime.InstallationTokenProvider")
    @mock.patch("agent_container.github_broker_runtime.BrokerSession.create")
    @mock.patch("agent_container.github_broker_runtime.GitHubAppMetadata.load")
    @mock.patch("agent_container.github_broker_runtime.load_broker_policy")
    def test_create_wires_issue_transport_to_shared_policy_and_token_provider(
        self,
        load_policy: mock.Mock,
        load_metadata: mock.Mock,
        create_session: mock.Mock,
        token_provider: mock.Mock,
        upload_transport: mock.Mock,
        receive_transport: mock.Mock,
        pr_transport: mock.Mock,
        issue_transport: mock.Mock,
    ) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        record = ProjectRecord(
            Repository.parse("jj1xgo/agent-container"), Path("/handovers")
        )
        policy = BrokerPolicy.create(
            project_id="agent-container",
            repository="jj1xgo/agent-container",
            repository_id=33,
            default_branch="main",
            protected_branches=("main",),
        )
        metadata = GitHubAppMetadata(
            client_id="Iv1abcdefghijk",
            installation_id=11,
            repository_id=22,
            private_key=Path("/private-key.pem"),
        )
        tokens = object()
        load_policy.return_value = policy
        load_metadata.return_value = metadata
        token_provider.return_value = tokens

        runtime = UploadPackBrokerRuntime.create(layout, record)

        issue_transport.assert_called_once_with(policy, tokens)
        pr_transport.assert_called_once_with(policy, tokens)
        upload_transport.assert_called_once_with(record.repository, tokens)
        receive_transport.assert_called_once_with(record.repository, tokens)
        token_provider.assert_called_once_with(
            GitHubAppMetadata(
                client_id="Iv1abcdefghijk",
                installation_id=11,
                repository_id=33,
                private_key=Path("/private-key.pem"),
            )
        )
        self.assertIs(runtime.issue_transport, issue_transport.return_value)
