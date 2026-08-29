import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from agent_container.github_app import GitHubAppMetadata
from agent_container.github_broker_runtime import UploadPackBrokerRuntime
from agent_container.github_broker_runtime import broker_token_metadata
from agent_container.github_broker_runtime import load_broker_policy
from agent_container.github_broker_runtime import write_broker_policy
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.state import ProjectRecord
from agent_container.state import Repository
from agent_container.state import StateLayout


class BrokerRuntimePolicyTest(unittest.TestCase):
    def test_loads_exact_repository_policy_with_confirmed_ruleset(self) -> None:
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

    def test_loads_bound_repository_policy_with_repository_id(self) -> None:
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

    def test_rejects_invalid_bound_ids_and_unknown_policy_keys(self) -> None:
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

            path.write_text(
                json.dumps(legacy | {"unexpected": True}), encoding="utf-8"
            )
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                load_broker_policy(path, record, "agent-container")

    def test_writes_only_bound_five_key_policy_schema(self) -> None:
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
                    "ruleset_confirmed": True,
                },
            )

    def test_rejects_unconfirmed_or_mismatched_policy(self) -> None:
        record = ProjectRecord(
            Repository.parse("jj1xgo/agent-container"), Path("/handovers")
        )
        with TemporaryDirectory() as temp:
            path = Path(temp) / "policy.json"
            for repository, confirmed in (
                ("jj1xgo/other", True),
                ("jj1xgo/agent-container", False),
            ):
                with self.subTest(repository=repository, confirmed=confirmed):
                    path.write_text(
                        json.dumps(
                            {
                                "repository": repository,
                                "default_branch": "main",
                                "protected_branches": ["main"],
                                "ruleset_confirmed": confirmed,
                            }
                        ),
                        encoding="utf-8",
                    )
                    path.chmod(0o600)
                    with self.assertRaises(ValueError):
                        load_broker_policy(path, record, "agent-container")


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
