"""Credential-free validation for shared GitHub scalar value types."""


MAX_ISSUE_NUMBER = 2_147_483_647


def validate_issue_number(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("issue number is invalid")
    if not 1 <= value <= MAX_ISSUE_NUMBER:
        raise ValueError("issue number is invalid")
    return value


def validate_repository_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("GitHub repository ID is invalid")
    return value
