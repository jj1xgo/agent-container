from dataclasses import dataclass
import re
from typing import Iterable

from agent_container.state import Repository
from agent_container.state import validate_project_id


ALLOWED_OPERATIONS = frozenset(
    {
        "git-upload-pack",
        "git-receive-pack",
        "pr-create",
        "pr-view",
        "pr-checks",
        "issue-list",
        "issue-view",
    }
)
MAX_PR_NUMBER = 2_147_483_647
MAX_ISSUE_NUMBER = 2_147_483_647
MAX_PR_TITLE_BYTES = 256
MAX_PR_BODY_BYTES = 65_536
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


def _validate_branch(value: str) -> str:
    if (
        not isinstance(value, str)
        or _BRANCH.fullmatch(value) is None
        or value.startswith("refs/")
        or value.endswith(("/", "."))
        or "//" in value
        or ".." in value
        or "@{" in value
        or any(
            part in {"", ".", ".."} or part.endswith(".lock")
            for part in value.split("/")
        )
    ):
        raise ValueError("branch is not allowed")
    return value


def _validate_text(
    value: str,
    *,
    name: str,
    maximum_bytes: int,
    allow_newline: bool,
    allow_empty: bool,
) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ValueError(f"{name} is invalid")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{name} is invalid")
    for character in value:
        codepoint = ord(character)
        if codepoint == 0 or codepoint == 127 or (
            codepoint < 32 and not (allow_newline and character in "\n\t")
        ):
            raise ValueError(f"{name} is invalid")
    return value


def validate_pr_number(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("pull request number is invalid")
    if not 1 <= value <= MAX_PR_NUMBER:
        raise ValueError("pull request number is invalid")
    return value


def validate_issue_number(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("issue number is invalid")
    if not 1 <= value <= MAX_ISSUE_NUMBER:
        raise ValueError("issue number is invalid")
    return value


def validate_pr_title(value: str) -> str:
    return _validate_text(
        value,
        name="pull request title",
        maximum_bytes=MAX_PR_TITLE_BYTES,
        allow_newline=False,
        allow_empty=False,
    )


def validate_pr_body(value: str) -> str:
    return _validate_text(
        value,
        name="pull request body",
        maximum_bytes=MAX_PR_BODY_BYTES,
        allow_newline=True,
        allow_empty=True,
    )


@dataclass(frozen=True)
class BrokerPolicy:
    project_id: str
    repository: Repository
    default_branch: str
    protected_branches: frozenset[str]

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        repository: str,
        default_branch: str,
        protected_branches: Iterable[str],
    ) -> "BrokerPolicy":
        validated_project = validate_project_id(project_id)
        validated_repository = Repository.parse(repository)
        validated_default = _validate_branch(default_branch)
        branches = tuple(_validate_branch(branch) for branch in protected_branches)
        if len(set(branches)) != len(branches):
            raise ValueError("protected branches must be unique")
        protected = frozenset(branches)
        if validated_default not in protected:
            raise ValueError("default branch must be protected")
        return cls(
            project_id=validated_project,
            repository=validated_repository,
            default_branch=validated_default,
            protected_branches=protected,
        )

    def validate_operation(self, operation: str) -> str:
        if operation not in ALLOWED_OPERATIONS:
            raise ValueError("operation is not allowed")
        return operation

    def validate_repository(self, repository: str) -> None:
        if repository != self.repository.slug:
            raise ValueError("repository is not allowed")

    def validate_work_branch(self, branch: str) -> str:
        try:
            validated = _validate_branch(branch)
        except (TypeError, ValueError):
            raise ValueError("branch is not allowed") from None
        if validated in self.protected_branches:
            raise ValueError("branch is protected")
        return validated

    def validate_push_ref(self, ref: str) -> str:
        prefix = "refs/heads/"
        if not isinstance(ref, str) or not ref.startswith(prefix):
            raise ValueError("push ref is not allowed")
        self.validate_work_branch(ref.removeprefix(prefix))
        return ref
