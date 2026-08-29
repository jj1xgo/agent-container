import unittest

from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_broker_policy import validate_issue_number
from agent_container.github_broker_policy import validate_pr_body
from agent_container.github_broker_policy import validate_pr_number
from agent_container.github_broker_policy import validate_pr_title


class BrokerPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = BrokerPolicy.create(
            project_id="agent-container",
            repository="jj1xgo/agent-container",
            default_branch="main",
            protected_branches=("main", "release/stable"),
        )

    def test_policy_is_project_and_repository_scoped(self) -> None:
        self.assertEqual(self.policy.project_id, "agent-container")
        self.assertEqual(self.policy.repository.slug, "jj1xgo/agent-container")
        self.assertEqual(self.policy.default_branch, "main")
        self.assertEqual(
            self.policy.protected_branches,
            frozenset({"main", "release/stable"}),
        )

    def test_policy_accepts_positive_repository_id(self) -> None:
        policy = BrokerPolicy.create(
            project_id="smoke",
            repository="jj1xgo/agent-container-smoke",
            repository_id=123,
            default_branch="main",
            protected_branches=("main",),
        )

        self.assertEqual(policy.repository_id, 123)

    def test_policy_rejects_invalid_repository_ids(self) -> None:
        for value in (True, False, 0, -1, "123", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BrokerPolicy.create(
                    project_id="smoke",
                    repository="jj1xgo/agent-container-smoke",
                    repository_id=value,
                    default_branch="main",
                    protected_branches=("main",),
                    require_repository_id=True,
                )

    def test_policy_accepts_missing_repository_id_only_for_legacy_policy(self) -> None:
        policy = BrokerPolicy.create(
            project_id="smoke",
            repository="jj1xgo/agent-container-smoke",
            repository_id=None,
            default_branch="main",
            protected_branches=("main",),
            require_repository_id=False,
        )

        self.assertIsNone(policy.repository_id)

    def test_only_fixed_operations_are_allowed(self) -> None:
        for operation in (
            "git-upload-pack",
            "git-receive-pack",
            "pr-create",
            "pr-view",
            "pr-checks",
        ):
            with self.subTest(operation=operation):
                self.assertEqual(self.policy.validate_operation(operation), operation)

        for operation in ("merge", "release", "gh-api", "workflow-dispatch", ""):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, "operation is not allowed"):
                    self.policy.validate_operation(operation)

    def test_allows_only_fixed_issue_read_operations(self) -> None:
        for operation in ("issue-list", "issue-view"):
            with self.subTest(operation=operation):
                self.assertEqual(self.policy.validate_operation(operation), operation)

        for operation in (
            "issue-create",
            "issue-edit",
            "issue-comment",
            "issue-close",
            "issue-lock",
            "issue-delete",
            "issue-search",
            "issue-query",
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError):
                    self.policy.validate_operation(operation)

    def test_request_repository_must_match_exactly(self) -> None:
        self.policy.validate_repository("jj1xgo/agent-container")

        for repository in (
            "jj1xgo/other",
            "JJ1XGO/agent-container",
            "https://github.com/jj1xgo/agent-container",
            "../agent-container",
        ):
            with self.subTest(repository=repository):
                with self.assertRaisesRegex(ValueError, "repository is not allowed"):
                    self.policy.validate_repository(repository)

    def test_work_branch_must_be_a_safe_unprotected_head(self) -> None:
        self.assertEqual(
            self.policy.validate_work_branch("feat/github-broker"),
            "feat/github-broker",
        )

        for branch in (
            "main",
            "release/stable",
            "refs/heads/feat/x",
            "../main",
            "feat//x",
            "feat/x.lock",
            "feat/x..y",
            "feat/x@{y",
            "-danger",
            "feat/white space",
        ):
            with self.subTest(branch=branch):
                with self.assertRaises(ValueError):
                    self.policy.validate_work_branch(branch)

    def test_ref_validation_only_accepts_working_heads(self) -> None:
        self.assertEqual(
            self.policy.validate_push_ref("refs/heads/feat/github-broker"),
            "refs/heads/feat/github-broker",
        )

        for ref in (
            "refs/heads/main",
            "refs/tags/v1",
            "HEAD",
            "refs/heads/feat//x",
        ):
            with self.subTest(ref=ref):
                with self.assertRaises(ValueError):
                    self.policy.validate_push_ref(ref)

    def test_invalid_policy_is_rejected(self) -> None:
        cases = (
            {"project_id": "../p"},
            {"repository": "not-a-repository"},
            {"default_branch": "refs/heads/main"},
            {"protected_branches": ("bad branch",)},
            {"protected_branches": ("release", "release")},
        )
        baseline = {
            "project_id": "p",
            "repository": "owner/repo",
            "default_branch": "main",
            "protected_branches": ("main",),
        }
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    BrokerPolicy.create(**(baseline | changes))

    def test_errors_do_not_echo_untrusted_marker(self) -> None:
        marker = "secret-capability-marker"
        for call in (
            lambda: self.policy.validate_operation(marker),
            lambda: self.policy.validate_repository(marker),
            lambda: self.policy.validate_work_branch(marker + " "),
        ):
            with self.subTest(call=call):
                with self.assertRaises(ValueError) as raised:
                    call()
                self.assertNotIn(marker, str(raised.exception))


class PullRequestInputTest(unittest.TestCase):
    def test_pr_number_is_bounded_positive_integer(self) -> None:
        self.assertEqual(validate_pr_number(1), 1)
        self.assertEqual(validate_pr_number(2_147_483_647), 2_147_483_647)
        for value in (True, 0, -1, 2_147_483_648, "1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_pr_number(value)  # type: ignore[arg-type]

    def test_pr_title_and_body_are_bounded_utf8_without_controls(self) -> None:
        self.assertEqual(validate_pr_title("設計を追加"), "設計を追加")
        self.assertEqual(validate_pr_body("概要\n\n- item"), "概要\n\n- item")

        for title in ("", "x" * 257, "bad\nline", "bad\x00value"):
            with self.subTest(title=title[:20]):
                with self.assertRaises(ValueError):
                    validate_pr_title(title)

        for body in ("x" * 65_537, "bad\x00value"):
            with self.subTest(length=len(body)):
                with self.assertRaises(ValueError):
                    validate_pr_body(body)


class IssueInputTest(unittest.TestCase):
    def test_validates_issue_number_without_accepting_boolean(self) -> None:
        self.assertEqual(validate_issue_number(1), 1)
        self.assertEqual(validate_issue_number(2_147_483_647), 2_147_483_647)
        for value in (True, 0, -1, 2_147_483_648, "1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_issue_number(value)  # type: ignore[arg-type]
