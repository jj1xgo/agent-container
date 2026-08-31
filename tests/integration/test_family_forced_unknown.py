"""Network-free host fixture for deterministic unknown-state reconciliation."""

import unittest

from tests.container.test_agentctl import AgentCtlFamilyTest


_FIXTURE_TESTS = (
    "test_approve_rejects_ineligible_and_expired_requests_without_send",
    "test_post_send_and_unclassified_uncertainty_become_unknown_without_retry",
    "test_resolve_created_verifies_supplied_issue_then_cleans_to_created",
    "test_resolve_not_created_warns_then_returns_only_to_pending",
)


def load_tests(
    loader: unittest.TestLoader,
    _standard_tests: unittest.TestSuite,
    _pattern: str | None,
) -> unittest.TestSuite:
    """Select the four bounded fake-boundary scenarios used by the operator gate."""

    suite = unittest.TestSuite()
    for name in _FIXTURE_TESTS:
        suite.addTest(AgentCtlFamilyTest(name))
    return suite


if __name__ == "__main__":
    unittest.main()
