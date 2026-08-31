"""Host-only orchestration for explicit family Issue approval commands."""

from dataclasses import dataclass
import json
from typing import Callable, Protocol, TextIO

from agent_container.family_github_app import FamilyInstallationTokenProvider
from agent_container.family_github_app import family_repository_transport
from agent_container.family_github_app import verify_family_repository
from agent_container.family_issue_create import CreatedIssue
from agent_container.family_issue_create import FamilyIssueCreator
from agent_container.family_issue_create import SendNotStarted
from agent_container.family_issue_create import SendOutcomeUnknown
from agent_container.family_pending import append_family_audit
from agent_container.family_pending import list_pending
from agent_container.family_pending import load_pending
from agent_container.family_pending import pending_lock
from agent_container.family_pending import PendingState
from agent_container.family_pending import transition_pending
from agent_container.family_state import FamilyBinding
from agent_container.family_state import FamilyStateLayout
from agent_container.family_state import load_family_binding
from agent_container.family_state import write_family_binding
from agent_container.github_app import GITHUB_API
from agent_container.github_app import GITHUB_API_VERSION
from agent_container.github_app import InstallationToken
from agent_container.github_values import validate_repository_id
from agent_container.state import ensure_private_directory
from agent_container.state import Repository


_INVENTORY_URL = f"{GITHUB_API}/installation/repositories?per_page=100"
_USER_AGENT = "agent-container-family-approval"


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("family repository inventory failed")
        result[key] = value
    return result


class FamilyInventory(Protocol):
    def resolve(
        self,
        repository: Repository,
        provider: FamilyInstallationTokenProvider,
    ) -> FamilyBinding: ...

    def verify(
        self,
        binding: FamilyBinding,
        provider: FamilyInstallationTokenProvider,
    ) -> None: ...


@dataclass(frozen=True)
class LiveFamilyInventory:
    """Bounded exact installation inventory used only by the host CLI."""

    transport: Callable = family_repository_transport

    def resolve(
        self,
        repository: Repository,
        provider: FamilyInstallationTokenProvider,
    ) -> FamilyBinding:
        token = provider.get()
        if type(token) is not InstallationToken:
            raise ValueError("family repository inventory failed")
        response = self.transport(
            "GET",
            _INVENTORY_URL,
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token.token}",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "User-Agent": _USER_AGENT,
            },
            None,
        )
        if response.status != 200 or any(
            name.lower() == "link" and value.strip()
            for name, value in response.headers.items()
        ):
            raise ValueError("family repository inventory failed")
        content_types = [
            value
            for name, value in response.headers.items()
            if name.lower() == "content-type"
        ]
        if (
            len(content_types) != 1
            or content_types[0].split(";", 1)[0].strip() != "application/json"
        ):
            raise ValueError("family repository inventory failed")
        try:
            payload = json.loads(
                response.body.decode("utf-8"),
                object_pairs_hook=_object_without_duplicates,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
            repositories = payload["repositories"]
            if (
                set(payload) != {"repositories", "total_count"}
                or type(payload["total_count"]) is not int
                or payload["total_count"] != 1
                or type(repositories) is not list
                or len(repositories) != 1
                or type(repositories[0]) is not dict
                or repositories[0]["full_name"] != repository.slug
            ):
                raise ValueError()
            repository_id = validate_repository_id(repositories[0]["id"])
        except (
            KeyError,
            RecursionError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
        ):
            raise ValueError("family repository inventory failed") from None
        return FamilyBinding(repository, repository_id)

    def verify(
        self,
        binding: FamilyBinding,
        provider: FamilyInstallationTokenProvider,
    ) -> None:
        verify_family_repository(provider.get(), binding, self.transport)


def _prepare_binding_directories(layout: FamilyStateLayout) -> None:
    ensure_private_directory(layout.root)
    for directory in (
        layout.family_root,
        layout.family_root / "projects",
        layout.family_project_dir,
        layout.family_pending_dir,
        layout.family_audit_file.parent,
    ):
        ensure_private_directory(directory, create=True)


def bind_family_project(
    layout: FamilyStateLayout,
    repository: Repository,
    *,
    provider_factory: Callable,
    inventory: FamilyInventory,
    stdout: TextIO,
) -> int:
    _prepare_binding_directories(layout)
    binding_path = layout.family_binding_file
    if binding_path.exists() or binding_path.is_symlink():
        load_family_binding(binding_path)
        raise ValueError("family project is already bound")
    provider = provider_factory(layout)
    try:
        binding = inventory.resolve(repository, provider)
    except Exception:
        raise ValueError("family repository inventory failed") from None
    if binding.repository != repository:
        raise ValueError("family repository inventory failed")
    write_family_binding(binding_path, binding, replace_existing=False)
    print(f"project: {layout.project_id}", file=stdout)
    print(f"repository: {binding.repository.slug}", file=stdout)
    print(f"repository-id: {binding.repository_id}", file=stdout)
    return 0


def _requests(layout: FamilyStateLayout):
    return list_pending(layout.family_pending_dir)


def print_family_list(layout: FamilyStateLayout, stdout: TextIO) -> int:
    binding = load_family_binding(layout.family_binding_file)
    requests = _requests(layout)
    print(f"project: {layout.project_id}", file=stdout)
    print(f"repository: {binding.repository.slug}", file=stdout)
    print(f"repository-id: {binding.repository_id}", file=stdout)
    print("request-id\tproject\tcreated-at\texpires-at\tstate", file=stdout)
    for request in requests:
        print(
            f"{request.request_id}\t{request.project_id}\t{request.created_at}"
            f"\t{request.expires_at}\t{request.state.value}",
            file=stdout,
        )
    return 0


def print_pending(layout: FamilyStateLayout, stdout: TextIO) -> int:
    print("request-id\tproject\tcreated-at\texpires-at\tstate", file=stdout)
    for request in _requests(layout):
        print(
            f"{request.request_id}\t{request.project_id}\t{request.created_at}"
            f"\t{request.expires_at}\t{request.state.value}",
            file=stdout,
        )
    return 0


def preview_family_issue(
    layout: FamilyStateLayout,
    request_id: str,
    stdout: TextIO,
    *,
    now: int,
) -> int:
    binding = load_family_binding(layout.family_binding_file)
    request = load_pending(layout.family_pending_dir, request_id)
    if request.issue is None or request.state not in {
        PendingState.PENDING,
        PendingState.UNKNOWN,
    }:
        raise ValueError("family pending request cannot be previewed")
    _audit(
        layout,
        request_id,
        now=now,
        operation="preview",
        status=request.state.value,
        stage="validation",
    )
    print(f"request-id: {request.request_id}", file=stdout)
    print(f"project: {request.project_id}", file=stdout)
    print(f"repository: {binding.repository.slug}", file=stdout)
    print(f"created-at: {request.created_at}", file=stdout)
    print(f"expires-at: {request.expires_at}", file=stdout)
    print(f"state: {request.state.value}", file=stdout)
    print("title:", file=stdout)
    print(request.issue.title, file=stdout)
    print("body:", file=stdout)
    print(request.issue.body, file=stdout)
    return 0


def _audit(
    layout: FamilyStateLayout,
    request_id: str,
    *,
    now: int,
    operation: str,
    status: str,
    stage: str,
) -> None:
    append_family_audit(
        layout.family_audit_file,
        timestamp=now,
        project_id=layout.project_id,
        request_id=request_id,
        operation=operation,
        status=status,
        stage=stage,
    )


def _expire_locked(layout: FamilyStateLayout, locked, *, now: int) -> None:
    transition_pending(locked, PendingState.EXPIRED)
    _audit(
        layout,
        locked.request.request_id,
        now=now,
        operation="expire",
        status="expired",
        stage="cleanup",
    )


def _approval_preview(
    layout: FamilyStateLayout,
    request_id: str,
    *,
    stdin: TextIO,
    stdout: TextIO,
    now: int,
):
    if not stdin.isatty():
        load_pending(layout.family_pending_dir, request_id)
        _audit(
            layout,
            request_id,
            now=now,
            operation="approve",
            status="denied",
            stage="validation",
        )
        print("approval refused: interactive TTY required", file=stdout)
        return None
    request = load_pending(layout.family_pending_dir, request_id)
    if request.state is not PendingState.PENDING or request.issue is None:
        raise ValueError("family pending request cannot be approved")
    if now >= request.expires_at:
        with pending_lock(layout.family_pending_dir, request_id) as locked:
            if locked.request != request:
                raise ValueError("family pending request changed")
            _expire_locked(layout, locked, now=now)
        raise ValueError("family pending request expired")
    binding = load_family_binding(layout.family_binding_file)
    print("External effect: create exactly one GitHub Issue", file=stdout)
    print(f"request-id: {request.request_id}", file=stdout)
    print(f"project: {request.project_id}", file=stdout)
    print(f"repository: {binding.repository.slug}", file=stdout)
    print("title:", file=stdout)
    print(request.issue.title, file=stdout)
    print("body:", file=stdout)
    print(request.issue.body, file=stdout)
    print(f"Type exactly: approve {request.request_id}", file=stdout)
    confirmation = stdin.readline()
    if confirmation != f"approve {request.request_id}\n":
        _audit(
            layout,
            request.request_id,
            now=now,
            operation="approve",
            status="denied",
            stage="validation",
        )
        print("approval cancelled", file=stdout)
        return None
    return request, binding


def approve_family_issue(
    layout: FamilyStateLayout,
    request_id: str,
    *,
    provider_factory: Callable,
    inventory: FamilyInventory,
    creator,
    stdin: TextIO,
    stdout: TextIO,
    now: int,
) -> int:
    preview = _approval_preview(
        layout,
        request_id,
        stdin=stdin,
        stdout=stdout,
        now=now,
    )
    if preview is None:
        return 1
    previewed_request, previewed_binding = preview
    creator = creator or FamilyIssueCreator()
    with pending_lock(layout.family_pending_dir, request_id) as locked:
        if (
            locked.request != previewed_request
            or locked.request.state is not PendingState.PENDING
            or locked.request.issue is None
        ):
            _audit(
                layout,
                request_id,
                now=now,
                operation="approve",
                status="denied",
                stage="validation",
            )
            raise ValueError("family pending request changed")
        if now >= locked.request.expires_at:
            _expire_locked(layout, locked, now=now)
            raise ValueError("family pending request expired")
        current_binding = load_family_binding(layout.family_binding_file)
        if current_binding != previewed_binding:
            _audit(
                layout,
                request_id,
                now=now,
                operation="approve",
                status="denied",
                stage="binding",
            )
            raise ValueError("family binding changed")
        try:
            provider = provider_factory(layout)
        except Exception:
            _audit(
                layout,
                request_id,
                now=now,
                operation="approve",
                status="denied",
                stage="token",
            )
            raise ValueError("family approval preflight failed") from None
        try:
            inventory.verify(current_binding, provider)
        except Exception:
            _audit(
                layout,
                request_id,
                now=now,
                operation="approve",
                status="denied",
                stage="inventory",
            )
            raise ValueError("family approval preflight failed") from None
        if load_family_binding(layout.family_binding_file) != previewed_binding:
            _audit(
                layout,
                request_id,
                now=now,
                operation="approve",
                status="denied",
                stage="binding",
            )
            raise ValueError("family binding changed")
        transition_pending(locked, PendingState.SENDING)
        try:
            _audit(
                layout,
                request_id,
                now=now,
                operation="approve",
                status="sending",
                stage="send",
            )
        except Exception:
            transition_pending(locked, PendingState.PENDING)
            raise ValueError("family approval audit failed") from None
        try:
            created = creator.create(
                current_binding,
                previewed_request.issue,
                provider,
            )
        except SendNotStarted as error:
            transition_pending(locked, PendingState.PENDING)
            _audit(
                layout,
                request_id,
                now=now,
                operation="approve",
                status="error",
                stage=error.stage,
            )
            raise ValueError("family Issue send did not start") from None
        except SendOutcomeUnknown as error:
            transition_pending(locked, PendingState.UNKNOWN)
            _audit(
                layout,
                request_id,
                now=now,
                operation="approve",
                status="unknown",
                stage=error.stage,
            )
            raise ValueError("family Issue send outcome is unknown") from None
        except Exception:
            transition_pending(locked, PendingState.UNKNOWN)
            _audit(
                layout,
                request_id,
                now=now,
                operation="approve",
                status="unknown",
                stage="send",
            )
            raise ValueError("family Issue send outcome is unknown") from None
        if (
            type(created) is not CreatedIssue
            or created.url
            != (
                f"https://github.com/{current_binding.repository.slug}/issues/"
                f"{created.number}"
            )
        ):
            transition_pending(locked, PendingState.UNKNOWN)
            _audit(
                layout,
                request_id,
                now=now,
                operation="approve",
                status="unknown",
                stage="response",
            )
            raise ValueError("family Issue send outcome is unknown")
        try:
            transition_pending(
                locked,
                PendingState.CREATED,
                issue_number=created.number,
                issue_url=created.url,
            )
        except Exception:
            try:
                _audit(
                    layout,
                    request_id,
                    now=now,
                    operation="approve",
                    status="error",
                    stage="cleanup",
                )
            except Exception:
                pass
            raise ValueError("family Issue cleanup failed") from None
        _audit(
            layout,
            request_id,
            now=now,
            operation="approve",
            status="created",
            stage="cleanup",
        )
    print(f"request-id: {request_id}", file=stdout)
    print(f"issue-number: {created.number}", file=stdout)
    print(f"issue-url: {created.url}", file=stdout)
    print("state: created", file=stdout)
    return 0


def reject_family_issue(
    layout: FamilyStateLayout,
    request_id: str,
    *,
    stdout: TextIO,
    now: int,
) -> int:
    with pending_lock(layout.family_pending_dir, request_id) as locked:
        if locked.request.state is not PendingState.PENDING:
            raise ValueError("family pending request cannot be rejected")
        if now >= locked.request.expires_at:
            _expire_locked(layout, locked, now=now)
            raise ValueError("family pending request expired")
        try:
            transition_pending(locked, PendingState.REJECTED)
        except Exception:
            try:
                _audit(
                    layout,
                    request_id,
                    now=now,
                    operation="reject",
                    status="error",
                    stage="cleanup",
                )
            except Exception:
                pass
            raise ValueError("family Issue cleanup failed") from None
        _audit(
            layout,
            request_id,
            now=now,
            operation="reject",
            status="rejected",
            stage="cleanup",
        )
    print(f"request-id: {request_id}", file=stdout)
    print("state: rejected", file=stdout)
    return 0


def resolve_created_family_issue(
    layout: FamilyStateLayout,
    request_id: str,
    issue_number: int,
    *,
    provider_factory: Callable,
    inventory: FamilyInventory,
    creator,
    stdout: TextIO,
    now: int,
) -> int:
    creator = creator or FamilyIssueCreator()
    with pending_lock(layout.family_pending_dir, request_id) as locked:
        if (
            locked.request.state is not PendingState.UNKNOWN
            or locked.request.issue is None
        ):
            raise ValueError("family pending request cannot be reconciled")
        binding = load_family_binding(layout.family_binding_file)
        try:
            provider = provider_factory(layout)
            inventory.verify(binding, provider)
        except Exception:
            _audit(
                layout,
                request_id,
                now=now,
                operation="resolve-created",
                status="error",
                stage="inventory",
            )
            raise ValueError("family Issue reconciliation failed") from None
        if load_family_binding(layout.family_binding_file) != binding:
            raise ValueError("family binding changed")
        try:
            created = creator.verify_existing(
                binding,
                locked.request.issue,
                issue_number,
                provider,
            )
        except Exception:
            _audit(
                layout,
                request_id,
                now=now,
                operation="resolve-created",
                status="error",
                stage="reconcile",
            )
            raise ValueError("family Issue reconciliation failed") from None
        expected_url = (
            f"https://github.com/{binding.repository.slug}/issues/{issue_number}"
        )
        if (
            type(created) is not CreatedIssue
            or created.number != issue_number
            or created.url != expected_url
        ):
            _audit(
                layout,
                request_id,
                now=now,
                operation="resolve-created",
                status="error",
                stage="reconcile",
            )
            raise ValueError("family Issue reconciliation failed")
        try:
            transition_pending(
                locked,
                PendingState.CREATED,
                issue_number=created.number,
                issue_url=created.url,
            )
        except Exception:
            try:
                _audit(
                    layout,
                    request_id,
                    now=now,
                    operation="resolve-created",
                    status="error",
                    stage="cleanup",
                )
            except Exception:
                pass
            raise ValueError("family Issue cleanup failed") from None
        _audit(
            layout,
            request_id,
            now=now,
            operation="resolve-created",
            status="created",
            stage="cleanup",
        )
    print(f"request-id: {request_id}", file=stdout)
    print(f"issue-number: {created.number}", file=stdout)
    print(f"issue-url: {created.url}", file=stdout)
    print("state: created", file=stdout)
    return 0


def resolve_not_created_family_issue(
    layout: FamilyStateLayout,
    request_id: str,
    *,
    stdin: TextIO,
    stdout: TextIO,
    now: int,
) -> int:
    request = load_pending(layout.family_pending_dir, request_id)
    if request.state is not PendingState.UNKNOWN or request.issue is None:
        raise ValueError("family pending request cannot be reconciled")
    if not stdin.isatty():
        _audit(
            layout,
            request_id,
            now=now,
            operation="resolve-not-created",
            status="denied",
            stage="reconcile",
        )
        print("reconciliation refused: interactive TTY required", file=stdout)
        return 1
    print(
        "WARNING: A later approve can create external state on GitHub.",
        file=stdout,
    )
    print(f"request-id: {request_id}", file=stdout)
    print(f"Type exactly: not-created {request_id}", file=stdout)
    if stdin.readline() != f"not-created {request_id}\n":
        _audit(
            layout,
            request_id,
            now=now,
            operation="resolve-not-created",
            status="denied",
            stage="reconcile",
        )
        print("reconciliation cancelled", file=stdout)
        return 1
    with pending_lock(layout.family_pending_dir, request_id) as locked:
        if locked.request != request or locked.request.state is not PendingState.UNKNOWN:
            raise ValueError("family pending request changed")
        transition_pending(locked, PendingState.PENDING)
        _audit(
            layout,
            request_id,
            now=now,
            operation="resolve-not-created",
            status="pending",
            stage="reconcile",
        )
    print("state: pending", file=stdout)
    return 0


def doctor_family(
    layout: FamilyStateLayout,
    *,
    provider_factory: Callable,
    stdout: TextIO,
) -> int:
    failures = False
    checks: list[tuple[str, str, str]] = []
    try:
        ensure_private_directory(layout.root)
        ensure_private_directory(layout.family_root)
        ensure_private_directory(layout.family_project_dir)
        checks.append(("PASS", "local-state", "valid"))
    except Exception:
        checks.append(("FAIL", "local-state", "invalid"))
        failures = True
    try:
        load_family_binding(layout.family_binding_file)
        checks.append(("PASS", "binding", "valid"))
    except Exception:
        checks.append(("FAIL", "binding", "invalid"))
        failures = True
    try:
        _requests(layout)
        checks.append(("PASS", "pending-invariants", "valid"))
    except Exception:
        checks.append(("FAIL", "pending-invariants", "invalid"))
        failures = True
    try:
        provider_factory(layout).get()
        checks.append(("PASS", "app-metadata-permissions", "valid"))
    except Exception:
        checks.append(("FAIL", "app-metadata-permissions", "invalid"))
        failures = True
    checks.append(("UNOBSERVED", "remote-availability", "not checked"))
    for level, name, detail in checks:
        print(f"{level}  {name}: {detail}", file=stdout)
    return 1 if failures else 0


def dispatch_family(
    arguments,
    layout: FamilyStateLayout,
    *,
    stdin: TextIO,
    stdout: TextIO,
    provider_factory: Callable | None,
    inventory: FamilyInventory | None,
    creator,
    now: int,
    approval_handler=approve_family_issue,
) -> int:
    provider_factory = provider_factory or FamilyInstallationTokenProvider
    inventory = inventory or LiveFamilyInventory()
    if arguments.family_command == "bind":
        repository = Repository.parse(arguments.repository)
        if repository.slug != arguments.repository.lower():
            raise ValueError("family repository must be canonical lowercase")
        return bind_family_project(
            layout,
            repository,
            provider_factory=provider_factory,
            inventory=inventory,
            stdout=stdout,
        )
    if arguments.family_command == "list":
        return print_family_list(layout, stdout)
    if arguments.family_command == "doctor":
        return doctor_family(
            layout,
            provider_factory=provider_factory,
            stdout=stdout,
        )
    operation = arguments.family_issue_command
    if operation == "pending":
        return print_pending(layout, stdout)
    if operation == "preview":
        return preview_family_issue(
            layout,
            arguments.request_id,
            stdout,
            now=now,
        )
    if operation == "approve":
        return approval_handler(
            layout,
            arguments.request_id,
            provider_factory=provider_factory,
            inventory=inventory,
            creator=creator,
            stdin=stdin,
            stdout=stdout,
            now=now,
        )
    if operation == "reject":
        return reject_family_issue(
            layout,
            arguments.request_id,
            stdout=stdout,
            now=now,
        )
    if operation == "resolve-created":
        return resolve_created_family_issue(
            layout,
            arguments.request_id,
            arguments.issue_number,
            provider_factory=provider_factory,
            inventory=inventory,
            creator=creator,
            stdout=stdout,
            now=now,
        )
    if operation == "resolve-not-created":
        return resolve_not_created_family_issue(
            layout,
            arguments.request_id,
            stdin=stdin,
            stdout=stdout,
            now=now,
        )
    raise ValueError("family Issue operation is not implemented")
