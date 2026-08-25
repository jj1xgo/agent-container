import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_container.github_broker_runtime import load_broker_policy
from agent_container.state import ProjectRecord
from agent_container.state import Repository


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
