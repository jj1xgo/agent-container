from datetime import datetime, timezone
import json
import unittest

from agent_container.github_app import HttpResponse
from agent_container.github_app import InstallationToken
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_pr import GitHubPullRequestTransport


class FakeTokens:
    def __init__(self) -> None:
        self.invalidations = 0

    def get(self) -> InstallationToken:
        return InstallationToken(
            "secret-installation-token",
            int(datetime(2026, 8, 25, 14, tzinfo=timezone.utc).timestamp()),
        )

    def invalidate(self) -> None:
        self.invalidations += 1


class GitHubPullRequestTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.calls = []
        self.responses = []
        policy = BrokerPolicy.create(
            project_id="agent-container",
            repository="jj1xgo/agent-container",
            default_branch="main",
            protected_branches=("main",),
        )

        def transport(method, url, headers, body):  # type: ignore[no-untyped-def]
            self.calls.append((method, url, dict(headers), body))
            return self.responses.pop(0)

        self.tokens = FakeTokens()
        self.client = GitHubPullRequestTransport(policy, self.tokens, transport)

    @staticmethod
    def response(payload, status=200):  # type: ignore[no-untyped-def]
        return HttpResponse(
            status, {"Content-Type": "application/json"}, json.dumps(payload).encode()
        )

    def test_create_uses_fixed_repository_and_returns_safe_summary(self) -> None:
        self.responses.append(
            self.response(
                {
                    "number": 12,
                    "state": "open",
                    "title": "Feature",
                    "html_url": "https://github.com/jj1xgo/agent-container/pull/12",
                    "body": "not returned",
                },
                201,
            )
        )
        result = self.client.create(
            base="main", head="feat/work", title="Feature", body="Body"
        )
        self.assertEqual(result["number"], 12)
        method, url, headers, body = self.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://api.github.com/repos/jj1xgo/agent-container/pulls")
        self.assertEqual(json.loads(body), {"base": "main", "body": "Body", "head": "feat/work", "title": "Feature"})
        self.assertIn("Authorization", headers)
        self.assertNotIn("body", result)

    def test_view_and_checks_use_only_numeric_pr_and_head_sha(self) -> None:
        summary = {
            "number": 12,
            "state": "open",
            "title": "Feature",
            "html_url": "https://github.com/jj1xgo/agent-container/pull/12",
        }
        self.responses.append(self.response(summary))
        self.assertEqual(self.client.view(12)["state"], "open")

        self.responses.extend(
            (
                self.response({"head": {"sha": "a" * 40}}),
                self.response(
                    {
                        "check_runs": [
                            {"name": "Unit tests", "status": "completed", "conclusion": "success"}
                        ]
                    }
                ),
            )
        )
        result = self.client.checks(12)
        self.assertEqual(result["checks"][0]["conclusion"], "success")
        self.assertEqual(self.calls[-1][1], f"https://api.github.com/repos/jj1xgo/agent-container/commits/{'a' * 40}/check-runs")

    def test_rejects_protected_head_wrong_base_and_secret_error_body(self) -> None:
        for base, head in (("develop", "feat/work"), ("main", "main")):
            with self.subTest(base=base, head=head), self.assertRaises(ValueError):
                self.client.create(base=base, head=head, title="Feature", body="")
        self.assertEqual(self.calls, [])

        self.responses.append(
            HttpResponse(403, {"Content-Type": "application/json"}, b"secret-marker")
        )
        with self.assertRaises(RuntimeError) as raised:
            self.client.view(12)
        self.assertNotIn("secret-marker", str(raised.exception))

    def test_retries_one_unauthorized_response(self) -> None:
        summary = {
            "number": 12,
            "state": "open",
            "title": "Feature",
            "html_url": "https://github.com/jj1xgo/agent-container/pull/12",
        }
        self.responses.extend(
            (
                self.response({"message": "secret-marker"}, 401),
                self.response(summary),
            )
        )
        self.assertEqual(self.client.view(12)["number"], 12)
        self.assertEqual(self.tokens.invalidations, 1)
        self.assertEqual(len(self.calls), 2)
