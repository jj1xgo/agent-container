"""Validation and canonical rendering for family Issue intake requests."""

from dataclasses import dataclass


TITLE_BYTES = 256
SUMMARY_BYTES = 2 * 1024
CONTEXT_BYTES = 4 * 1024
CRITERION_BYTES = 512
MAX_CRITERIA = 20

_REQUEST_FIELDS = frozenset(
    {"title", "summary", "context", "acceptance_criteria"}
)
_BIDI_OVERRIDES = frozenset(
    (*range(0x202A, 0x202F), *range(0x2066, 0x206A))
)
_MARKDOWN_ESCAPES = frozenset("\\`*{}[]()#+-.!_>~|<=")


@dataclass(frozen=True)
class FamilyIssueDraft:
    title: str
    summary: str
    context: str
    acceptance_criteria: tuple[str, ...]


@dataclass(frozen=True)
class CanonicalFamilyIssue:
    title: str
    body: str


def _invalid() -> ValueError:
    return ValueError("family issue draft is invalid")


def _validate_text(value: object, maximum_bytes: int) -> str:
    if type(value) is not str or not value:
        raise _invalid()
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid() from None
    if len(encoded) > maximum_bytes:
        raise _invalid()
    for character in value:
        codepoint = ord(character)
        if codepoint <= 0x1F or 0x7F <= codepoint <= 0x9F:
            raise _invalid()
        if codepoint in _BIDI_OVERRIDES:
            raise _invalid()
    return value


def _validate_draft(draft: object) -> FamilyIssueDraft:
    if type(draft) is not FamilyIssueDraft:
        raise _invalid()
    _validate_text(draft.title, TITLE_BYTES)
    _validate_text(draft.summary, SUMMARY_BYTES)
    _validate_text(draft.context, CONTEXT_BYTES)
    if type(draft.acceptance_criteria) is not tuple:
        raise _invalid()
    if not 1 <= len(draft.acceptance_criteria) <= MAX_CRITERIA:
        raise _invalid()
    for criterion in draft.acceptance_criteria:
        _validate_text(criterion, CRITERION_BYTES)
    return draft


def _escape_markdown_literal(value: str) -> str:
    return "".join(
        f"\\{character}" if character in _MARKDOWN_ESCAPES else character
        for character in value
    )


def parse_family_issue_draft(payload: object) -> FamilyIssueDraft:
    """Parse and validate the exact credential-free request content schema."""

    if type(payload) is not dict or set(payload) != _REQUEST_FIELDS:
        raise _invalid()
    criteria = payload["acceptance_criteria"]
    if type(criteria) is not list:
        raise _invalid()
    draft = FamilyIssueDraft(
        _validate_text(payload["title"], TITLE_BYTES),
        _validate_text(payload["summary"], SUMMARY_BYTES),
        _validate_text(payload["context"], CONTEXT_BYTES),
        tuple(criteria),
    )
    return _validate_draft(draft)


def render_family_issue_body(draft: FamilyIssueDraft) -> str:
    """Render a validated draft with headings and bullets owned by the host."""

    draft = _validate_draft(draft)
    summary = _escape_markdown_literal(draft.summary)
    context = _escape_markdown_literal(draft.context)
    criteria = "".join(
        f"- {_escape_markdown_literal(criterion)}\n"
        for criterion in draft.acceptance_criteria
    )
    return (
        f"## Summary\n\n{summary}\n\n"
        f"## Context\n\n{context}\n\n"
        f"## Acceptance criteria\n\n{criteria}"
    )


def canonicalize_family_issue(draft: FamilyIssueDraft) -> CanonicalFamilyIssue:
    """Return the immutable title/body representation used by preview and POST."""

    draft = _validate_draft(draft)
    return CanonicalFamilyIssue(draft.title, render_family_issue_body(draft))
