"""Private persistence and lifecycle for host-approved family Issues."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
import fcntl
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterator

from agent_container.family_issue import CanonicalFamilyIssue
from agent_container.family_state import _CLOEXEC
from agent_container.family_state import _entry_stat
from agent_container.family_state import _NOFOLLOW
from agent_container.family_state import _open_private_file
from agent_container.family_state import _open_private_parent
from agent_container.family_state import _rename_exchange
from agent_container.family_state import _same_inode
from agent_container.family_state import _temporary_name
from agent_container.family_state import _unlink_owned
from agent_container.family_state import _validate_private_file
from agent_container.family_state import _write_all
from agent_container.github_values import validate_issue_number
from agent_container.state import validate_project_id


_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
_RECORD_NAME = re.compile(r"^(?P<request_id>[0-9a-f]{32})\.json$")
_LOCK_NAME = re.compile(r"^\.(?P<request_id>[0-9a-f]{32})\.json\.lock$")
_TTL_SECONDS = 24 * 60 * 60
_MAX_UNFINISHED = 10
_MAX_CANONICAL_BODY_BYTES = 64 * 1024
_MAX_RECORD_BYTES = 128 * 1024
_MAX_AUDIT_BYTES = 4 * 1024 * 1024
_ID_ATTEMPTS = 32
_STORE_LOCK = ".pending.lock"
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

_AUDIT_OPERATIONS = frozenset(
    {
        "intake",
        "preview",
        "approve",
        "reject",
        "expire",
        "resolve-created",
        "resolve-not-created",
        "recover",
    }
)
_AUDIT_STATUSES = frozenset(
    {
        "pending",
        "sending",
        "created",
        "rejected",
        "expired",
        "unknown",
        "denied",
        "error",
    }
)
_AUDIT_STAGES = frozenset(
    {
        "intake",
        "validation",
        "binding",
        "token",
        "inventory",
        "send",
        "response",
        "cleanup",
        "reconcile",
    }
)


class PendingCapacityError(ValueError):
    pass


class PendingState(str, Enum):
    PENDING = "pending"
    SENDING = "sending"
    CREATED = "created"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


_CONTENT_STATES = frozenset(
    {PendingState.PENDING, PendingState.SENDING, PendingState.UNKNOWN}
)
_TERMINAL_STATES = frozenset(
    {PendingState.CREATED, PendingState.REJECTED, PendingState.EXPIRED}
)
_TRANSITIONS = {
    PendingState.PENDING: frozenset(
        {PendingState.SENDING, PendingState.REJECTED, PendingState.EXPIRED}
    ),
    PendingState.SENDING: frozenset(
        {PendingState.PENDING, PendingState.CREATED, PendingState.UNKNOWN}
    ),
    PendingState.UNKNOWN: frozenset(
        {PendingState.PENDING, PendingState.CREATED}
    ),
    PendingState.CREATED: frozenset(),
    PendingState.REJECTED: frozenset(),
    PendingState.EXPIRED: frozenset(),
}


@dataclass(frozen=True)
class PendingRequest:
    request_id: str
    project_id: str
    created_at: int
    expires_at: int
    state: PendingState
    issue: CanonicalFamilyIssue | None = field(repr=False)
    issue_number: int | None = None
    issue_url: str | None = None


_LOCK_CONSTRUCTOR = object()


class LockedPending:
    """Opaque validated request snapshot whose exclusive flock is still held."""

    __slots__ = (
        "_active",
        "_expected",
        "_parent_descriptor",
        "_record_name",
        "_request",
    )

    def __init__(
        self,
        token: object,
        parent_descriptor: int,
        record_name: str,
        expected: os.stat_result,
        request: PendingRequest,
    ) -> None:
        if token is not _LOCK_CONSTRUCTOR:
            raise TypeError("locked pending handles cannot be constructed")
        self._active = True
        self._parent_descriptor = parent_descriptor
        self._record_name = record_name
        self._expected = expected
        self._request = request

    @property
    def request(self) -> PendingRequest:
        if not self._active:
            raise ValueError("pending request lock is closed")
        return self._request


def _invalid() -> ValueError:
    return ValueError("family pending record is invalid")


def _validate_request_id(value: object) -> str:
    if type(value) is not str or _REQUEST_ID.fullmatch(value) is None:
        raise _invalid()
    return value


def _exact_integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _invalid()
    return value


def _exact_text(value: object) -> str:
    if type(value) is not str:
        raise _invalid()
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid() from None
    return value


def _without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _validate_issue_url(value: object, issue_number: int) -> str:
    url = _exact_text(value)
    prefix = "https://github.com/"
    suffix = f"/issues/{issue_number}"
    if not url.startswith(prefix) or not url.endswith(suffix):
        raise _invalid()
    repository = url[len(prefix) : -len(suffix)]
    parts = repository.split("/")
    if len(parts) != 2 or any(
        not part
        or part in {".", ".."}
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", part) is None
        for part in parts
    ):
        raise _invalid()
    return url


def _request_payload(request: PendingRequest) -> dict[str, object]:
    payload: dict[str, object] = {
        "created_at": request.created_at,
        "expires_at": request.expires_at,
        "project_id": request.project_id,
        "request_id": request.request_id,
        "state": request.state.value,
    }
    if request.state in _CONTENT_STATES:
        if type(request.issue) is not CanonicalFamilyIssue:
            raise _invalid()
        payload["body"] = request.issue.body
        payload["title"] = request.issue.title
    elif request.issue is not None:
        raise _invalid()
    if request.state is PendingState.CREATED:
        if request.issue_number is None or request.issue_url is None:
            raise _invalid()
        payload["issue_number"] = request.issue_number
        payload["issue_url"] = request.issue_url
    elif request.issue_number is not None or request.issue_url is not None:
        raise _invalid()
    return payload


def _validate_request(request: PendingRequest) -> PendingRequest:
    if type(request) is not PendingRequest:
        raise _invalid()
    request_id = _validate_request_id(request.request_id)
    try:
        project_id = validate_project_id(request.project_id)
    except (TypeError, ValueError):
        raise _invalid() from None
    created_at = _exact_integer(request.created_at)
    expires_at = _exact_integer(request.expires_at)
    if expires_at != created_at + _TTL_SECONDS or type(request.state) is not PendingState:
        raise _invalid()
    issue = request.issue
    issue_number = request.issue_number
    issue_url = request.issue_url
    if request.state in _CONTENT_STATES:
        if type(issue) is not CanonicalFamilyIssue:
            raise _invalid()
        title = _exact_text(issue.title)
        body = _exact_text(issue.body)
        if not title or len(title.encode("utf-8")) > 256:
            raise _invalid()
        if not body or len(body.encode("utf-8")) > _MAX_CANONICAL_BODY_BYTES:
            raise _invalid()
        issue = CanonicalFamilyIssue(title, body)
        if issue_number is not None or issue_url is not None:
            raise _invalid()
    elif issue is not None:
        raise _invalid()
    if request.state is PendingState.CREATED:
        try:
            issue_number = validate_issue_number(issue_number)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise _invalid() from None
        issue_url = _validate_issue_url(issue_url, issue_number)
    elif issue_number is not None or issue_url is not None:
        raise _invalid()
    return PendingRequest(
        request_id,
        project_id,
        created_at,
        expires_at,
        request.state,
        issue,
        issue_number,
        issue_url,
    )


def _encode_request(request: PendingRequest) -> bytes:
    request = _validate_request(request)
    return (
        json.dumps(
            _request_payload(request),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _decode_request(body: bytes) -> PendingRequest:
    try:
        payload = json.loads(
            body.decode("ascii"),
            object_pairs_hook=_without_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(_invalid()),
        )
        if not isinstance(payload, dict):
            raise _invalid()
        common = {
            "created_at",
            "expires_at",
            "project_id",
            "request_id",
            "state",
        }
        state = PendingState(payload.get("state"))
        if state in _CONTENT_STATES:
            if set(payload) != common | {"body", "title"}:
                raise _invalid()
            issue: CanonicalFamilyIssue | None = CanonicalFamilyIssue(
                _exact_text(payload["title"]),
                _exact_text(payload["body"]),
            )
            issue_number = None
            issue_url = None
        elif state is PendingState.CREATED:
            if set(payload) != common | {"issue_number", "issue_url"}:
                raise _invalid()
            issue = None
            issue_number = payload["issue_number"]
            issue_url = payload["issue_url"]
        else:
            if set(payload) != common:
                raise _invalid()
            issue = None
            issue_number = None
            issue_url = None
        return _validate_request(
            PendingRequest(
                _validate_request_id(payload["request_id"]),
                payload["project_id"],
                payload["created_at"],
                payload["expires_at"],
                state,
                issue,
                issue_number,
                issue_url,
            )
        )
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _invalid() from None


def _read_record(
    parent_descriptor: int, record_name: str
) -> tuple[PendingRequest, os.stat_result]:
    descriptor, expected = _open_private_file(parent_descriptor, record_name)
    try:
        body = bytearray()
        while len(body) <= _MAX_RECORD_BYTES:
            chunk = os.read(
                descriptor,
                min(4096, _MAX_RECORD_BYTES + 1 - len(body)),
            )
            if not chunk:
                break
            body.extend(chunk)
        if len(body) > _MAX_RECORD_BYTES:
            raise _invalid()
    finally:
        os.close(descriptor)
    current = _entry_stat(parent_descriptor, record_name)
    _validate_private_file(current)
    if not _same_inode(expected, current):
        raise ValueError("family pending record changed")
    return _decode_request(bytes(body)), expected


def _open_lock(parent_descriptor: int, lock_name: str) -> int:
    try:
        descriptor = os.open(
            lock_name,
            os.O_RDWR | os.O_CREAT | _NOFOLLOW | _CLOEXEC,
            0o600,
            dir_fd=parent_descriptor,
        )
    except OSError:
        raise ValueError("family pending lock is invalid") from None
    try:
        opened = os.fstat(descriptor)
        _validate_private_file(opened)
        current = os.stat(
            lock_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        _validate_private_file(current)
        if not _same_inode(opened, current):
            raise ValueError("family pending lock changed")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        current = os.stat(
            lock_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        _validate_private_file(current)
        if not _same_inode(opened, current):
            raise ValueError("family pending lock changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _inventory(parent_descriptor: int) -> list[str]:
    try:
        entries = os.listdir(parent_descriptor)
    except OSError:
        raise ValueError("family pending inventory is invalid") from None
    records: set[str] = set()
    locks: set[str] = set()
    for name in entries:
        record = _RECORD_NAME.fullmatch(name)
        if record is not None:
            records.add(record.group("request_id"))
        else:
            lock = _LOCK_NAME.fullmatch(name)
            if lock is not None:
                locks.add(lock.group("request_id"))
            elif name != _STORE_LOCK:
                raise ValueError("family pending inventory is invalid")
        _validate_private_file(_entry_stat(parent_descriptor, name))
    if not locks.issubset(records):
        raise ValueError("family pending inventory is invalid")
    return sorted(records)


def _prove_cleanup(
    parent_descriptor: int,
    temporary: str,
    retained_descriptor: int,
    expected: os.stat_result,
    expected_links: int,
) -> None:
    retained = os.fstat(retained_descriptor)
    if not _same_inode(expected, retained) or retained.st_nlink != expected_links:
        raise ValueError("family pending content cleanup is incomplete")
    try:
        os.stat(
            temporary,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        pass
    else:
        raise ValueError("family pending content cleanup is incomplete")
    _inventory(parent_descriptor)


def _publish_new(parent_descriptor: int, record_name: str, body: bytes) -> None:
    temporary = _temporary_name(record_name)
    descriptor: int | None = None
    temporary_stat: os.stat_result | None = None
    published = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
            0o600,
            dir_fd=parent_descriptor,
        )
        temporary_stat = os.fstat(descriptor)
        os.fchmod(descriptor, 0o600)
        _validate_private_file(os.fstat(descriptor))
        _write_all(descriptor, body)
        os.fsync(descriptor)
        os.link(
            temporary,
            record_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        published = True
        _unlink_owned(parent_descriptor, temporary, temporary_stat)
        _prove_cleanup(
            parent_descriptor,
            temporary,
            descriptor,
            temporary_stat,
            1,
        )
        os.fsync(parent_descriptor)
        _prove_cleanup(
            parent_descriptor,
            temporary,
            descriptor,
            temporary_stat,
            1,
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_stat is not None and not published:
            _unlink_owned(parent_descriptor, temporary, temporary_stat)


def _atomic_replace(
    parent_descriptor: int,
    record_name: str,
    expected: os.stat_result,
    body: bytes,
) -> os.stat_result:
    temporary = _temporary_name(record_name)
    descriptor: int | None = None
    displaced_descriptor: int | None = None
    temporary_stat: os.stat_result | None = None
    published = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
            0o600,
            dir_fd=parent_descriptor,
        )
        temporary_stat = os.fstat(descriptor)
        os.fchmod(descriptor, 0o600)
        _validate_private_file(os.fstat(descriptor))
        _write_all(descriptor, body)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        current = _entry_stat(parent_descriptor, record_name)
        _validate_private_file(current)
        if not _same_inode(expected, current):
            raise ValueError("family pending record changed")
        displaced_descriptor, displaced = _open_private_file(
            parent_descriptor, record_name
        )
        if not _same_inode(expected, displaced):
            raise ValueError("family pending record changed")
        os.fsync(parent_descriptor)
        _rename_exchange(parent_descriptor, temporary, record_name)
        replaced = _entry_stat(parent_descriptor, temporary)
        if not _same_inode(expected, replaced):
            _rename_exchange(parent_descriptor, temporary, record_name)
            raise ValueError("family pending record changed")
        published = True
        _unlink_owned(parent_descriptor, temporary, expected)
        _prove_cleanup(
            parent_descriptor,
            temporary,
            displaced_descriptor,
            expected,
            0,
        )
        os.fsync(parent_descriptor)
        _prove_cleanup(
            parent_descriptor,
            temporary,
            displaced_descriptor,
            expected,
            0,
        )
        current = _entry_stat(parent_descriptor, record_name)
        _validate_private_file(current)
        if temporary_stat is None or not _same_inode(temporary_stat, current):
            raise ValueError("family pending record changed")
        return current
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if displaced_descriptor is not None:
            os.close(displaced_descriptor)
        if temporary_stat is not None and not published:
            _unlink_owned(parent_descriptor, temporary, temporary_stat)


def create_pending(
    store: Path,
    project_id: str,
    issue: CanonicalFamilyIssue,
    *,
    now: int,
    random_bytes: Callable[[int], bytes] = os.urandom,
) -> PendingRequest:
    """Create one durable pending record without overwriting any sibling."""

    try:
        validated_project = validate_project_id(project_id)
    except (TypeError, ValueError):
        raise _invalid() from None
    created_at = _exact_integer(now)
    prototype = PendingRequest(
        "0" * 32,
        validated_project,
        created_at,
        created_at + _TTL_SECONDS,
        PendingState.PENDING,
        issue,
    )
    _validate_request(prototype)
    parent_descriptor = _open_private_parent(store / "record")
    store_lock: int | None = None
    try:
        store_lock = _open_lock(parent_descriptor, _STORE_LOCK)
        records = _inventory(parent_descriptor)
        unfinished = 0
        for request_id in records:
            request, _metadata = _read_record(
                parent_descriptor, f"{request_id}.json"
            )
            if request.state in _CONTENT_STATES:
                unfinished += 1
        if unfinished >= _MAX_UNFINISHED:
            raise PendingCapacityError("family pending inventory is full")
        for _attempt in range(_ID_ATTEMPTS):
            generated = random_bytes(16)
            if type(generated) is not bytes or len(generated) != 16:
                raise ValueError("family pending random source is invalid")
            request_id = generated.hex()
            record_name = f"{request_id}.json"
            try:
                os.stat(
                    record_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                request = PendingRequest(
                    request_id,
                    validated_project,
                    created_at,
                    created_at + _TTL_SECONDS,
                    PendingState.PENDING,
                    issue,
                )
                try:
                    _publish_new(
                        parent_descriptor,
                        record_name,
                        _encode_request(request),
                    )
                except FileExistsError:
                    continue
                return _validate_request(request)
        raise FileExistsError("could not allocate family pending request")
    finally:
        if store_lock is not None:
            os.close(store_lock)
        os.close(parent_descriptor)


@contextmanager
def pending_lock(store: Path, request_id: str) -> Iterator[LockedPending]:
    """Hold the request's validated flock until approval/reconciliation completes."""

    validated_id = _validate_request_id(request_id)
    record_name = f"{validated_id}.json"
    parent_descriptor = _open_private_parent(store / "record")
    lock_descriptor: int | None = None
    locked: LockedPending | None = None
    try:
        _inventory(parent_descriptor)
        record_metadata = _entry_stat(parent_descriptor, record_name)
        _validate_private_file(record_metadata)
        lock_descriptor = _open_lock(
            parent_descriptor, f".{record_name}.lock"
        )
        _inventory(parent_descriptor)
        request, expected = _read_record(parent_descriptor, record_name)
        if request.request_id != validated_id:
            raise _invalid()
        locked = LockedPending(
            _LOCK_CONSTRUCTOR,
            parent_descriptor,
            record_name,
            expected,
            request,
        )
        yield locked
    finally:
        if locked is not None:
            locked._active = False
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        os.close(parent_descriptor)


def load_pending(store: Path, request_id: str) -> PendingRequest:
    with pending_lock(store, request_id) as locked:
        return locked.request


def list_pending(store: Path) -> tuple[PendingRequest, ...]:
    parent_descriptor = _open_private_parent(store / "record")
    store_lock: int | None = None
    try:
        store_lock = _open_lock(parent_descriptor, _STORE_LOCK)
        request_ids = _inventory(parent_descriptor)
    finally:
        if store_lock is not None:
            os.close(store_lock)
        os.close(parent_descriptor)
    requests = [load_pending(store, request_id) for request_id in request_ids]
    return tuple(sorted(requests, key=lambda item: (item.created_at, item.request_id)))


def initialize_pending_store(store: Path) -> tuple[PendingRequest, ...]:
    """Validate a stable inventory and recover every interrupted send."""

    parent_descriptor = _open_private_parent(store / "record")
    store_lock: int | None = None
    try:
        store_lock = _open_lock(parent_descriptor, _STORE_LOCK)
        request_ids = _inventory(parent_descriptor)
        requests: list[PendingRequest] = []
        for request_id in request_ids:
            with pending_lock(store, request_id) as locked:
                if locked.request.state is PendingState.SENDING:
                    requests.append(
                        transition_pending(locked, PendingState.UNKNOWN)
                    )
                else:
                    requests.append(locked.request)
        return tuple(
            sorted(requests, key=lambda item: (item.created_at, item.request_id))
        )
    finally:
        if store_lock is not None:
            os.close(store_lock)
        os.close(parent_descriptor)


def transition_pending(
    locked: LockedPending,
    target: PendingState,
    *,
    issue_number: int | None = None,
    issue_url: str | None = None,
) -> PendingRequest:
    """Durably transition a request while its caller-owned flock remains held."""

    if type(locked) is not LockedPending or not locked._active:
        raise ValueError("pending request lock is invalid")
    if type(target) is not PendingState or target not in _TRANSITIONS[locked._request.state]:
        raise ValueError("family pending transition is invalid")
    current = _entry_stat(locked._parent_descriptor, locked._record_name)
    _validate_private_file(current)
    if not _same_inode(locked._expected, current):
        raise ValueError("family pending record changed")
    observed, observed_stat = _read_record(
        locked._parent_descriptor, locked._record_name
    )
    if observed != locked._request or not _same_inode(locked._expected, observed_stat):
        raise ValueError("family pending record changed")
    if target is PendingState.CREATED:
        try:
            validated_number = validate_issue_number(issue_number)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError("family pending created result is invalid") from None
        validated_url = _validate_issue_url(issue_url, validated_number)
    else:
        if issue_number is not None or issue_url is not None:
            raise ValueError("family pending created result is invalid")
        validated_number = None
        validated_url = None
    changed = _validate_request(
        PendingRequest(
            observed.request_id,
            observed.project_id,
            observed.created_at,
            observed.expires_at,
            target,
            observed.issue if target in _CONTENT_STATES else None,
            validated_number,
            validated_url,
        )
    )
    locked._expected = _atomic_replace(
        locked._parent_descriptor,
        locked._record_name,
        locked._expected,
        _encode_request(changed),
    )
    locked._request = changed
    return changed


def expire_pending(store: Path, request_id: str, *, now: int) -> PendingRequest:
    timestamp = _exact_integer(now)
    with pending_lock(store, request_id) as locked:
        if (
            locked.request.state is not PendingState.PENDING
            or timestamp < locked.request.expires_at
        ):
            raise ValueError("family pending request cannot expire")
        return transition_pending(locked, PendingState.EXPIRED)


def recover_sending(store: Path, request_id: str) -> PendingRequest:
    with pending_lock(store, request_id) as locked:
        if locked.request.state is not PendingState.SENDING:
            raise ValueError("family pending request cannot be recovered")
        return transition_pending(locked, PendingState.UNKNOWN)


@dataclass(frozen=True)
class _AuditEvent:
    timestamp: int
    project_id: str
    request_id: str
    operation: str
    status: str
    stage: str

    def validated(self) -> "_AuditEvent":
        if type(self.timestamp) is not int or self.timestamp < 0:
            raise ValueError("family audit event is invalid")
        try:
            project_id = validate_project_id(self.project_id)
            request_id = _validate_request_id(self.request_id)
        except (TypeError, ValueError):
            raise ValueError("family audit event is invalid") from None
        if (
            type(self.operation) is not str
            or self.operation not in _AUDIT_OPERATIONS
            or type(self.status) is not str
            or self.status not in _AUDIT_STATUSES
            or type(self.stage) is not str
            or self.stage not in _AUDIT_STAGES
        ):
            raise ValueError("family audit event is invalid")
        return _AuditEvent(
            self.timestamp,
            project_id,
            request_id,
            self.operation,
            self.status,
            self.stage,
        )

    def encode(self) -> bytes:
        event = self.validated()
        return (
            json.dumps(
                {
                    "operation": event.operation,
                    "project_id": event.project_id,
                    "request_id": event.request_id,
                    "stage": event.stage,
                    "status": event.status,
                    "timestamp": event.timestamp,
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")


def _decode_audit_line(body: bytes) -> _AuditEvent:
    try:
        payload = json.loads(
            body.decode("ascii"),
            object_pairs_hook=_without_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        if not isinstance(payload, dict) or set(payload) != {
            "operation",
            "project_id",
            "request_id",
            "stage",
            "status",
            "timestamp",
        }:
            raise ValueError()
        return _AuditEvent(
            payload["timestamp"],
            payload["project_id"],
            payload["request_id"],
            payload["operation"],
            payload["status"],
            payload["stage"],
        ).validated()
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("family audit file is invalid") from None


def _validate_audit_contents(descriptor: int) -> int:
    size = os.fstat(descriptor).st_size
    if size > _MAX_AUDIT_BYTES:
        raise ValueError("family audit file is invalid")
    body = bytearray()
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, min(4096, size - offset), offset)
        if not chunk:
            raise ValueError("family audit file is invalid")
        body.extend(chunk)
        offset += len(chunk)
    if body and body[-1:] != b"\n":
        raise ValueError("family audit file is invalid")
    for line in body.splitlines():
        _decode_audit_line(bytes(line))
    return size


def _open_audit(parent_descriptor: int, name: str) -> tuple[int, int]:
    flags = os.O_RDWR | os.O_APPEND | _NOFOLLOW | _CLOEXEC | _NONBLOCK
    created = False
    try:
        descriptor = os.open(
            name,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_descriptor,
        )
        created = True
    except FileExistsError:
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        except OSError:
            raise ValueError("family audit file is invalid") from None
    except OSError:
        raise ValueError("family audit file is invalid") from None
    try:
        if created:
            os.fchmod(descriptor, 0o600)
        opened = os.fstat(descriptor)
        _validate_private_file(opened)
        current = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        _validate_private_file(current)
        if not _same_inode(opened, current):
            raise ValueError("family audit file changed")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        current = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        _validate_private_file(current)
        if not _same_inode(opened, current):
            raise ValueError("family audit file changed")
        return descriptor, _validate_audit_contents(descriptor)
    except BaseException:
        os.close(descriptor)
        raise


def _require_audit_identity(
    parent_descriptor: int, name: str, descriptor: int
) -> None:
    try:
        opened = os.fstat(descriptor)
        _validate_private_file(opened)
        current = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        _validate_private_file(current)
    except (OSError, PermissionError, ValueError):
        raise ValueError("family audit file changed") from None
    if not _same_inode(opened, current):
        raise ValueError("family audit file changed")


def _audit_identity_matches(
    parent_descriptor: int, name: str, descriptor: int
) -> bool:
    try:
        opened = os.fstat(descriptor)
        _validate_private_file(opened)
        current = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        _validate_private_file(current)
    except (OSError, PermissionError, ValueError):
        return False
    return _same_inode(opened, current)


def _rollback_audit_append(
    parent_descriptor: int,
    name: str,
    descriptor: int,
    original_size: int,
) -> None:
    if not _audit_identity_matches(parent_descriptor, name, descriptor):
        return
    os.ftruncate(descriptor, original_size)
    os.fsync(descriptor)
    _require_audit_identity(parent_descriptor, name, descriptor)
    os.fsync(parent_descriptor)
    _require_audit_identity(parent_descriptor, name, descriptor)


def append_family_audit(
    path: Path,
    *,
    timestamp: int,
    project_id: str,
    request_id: str,
    operation: str,
    status: str,
    stage: str,
) -> None:
    """Append one locked, durable, fixed-schema, content-free event."""

    body = _AuditEvent(
        timestamp,
        project_id,
        request_id,
        operation,
        status,
        stage,
    ).encode()
    parent_descriptor = _open_private_parent(path)
    descriptor: int | None = None
    original_size: int | None = None
    try:
        descriptor, original_size = _open_audit(parent_descriptor, path.name)
        if original_size + len(body) > _MAX_AUDIT_BYTES:
            raise ValueError("family audit file is invalid")
        try:
            _write_all(descriptor, body)
            os.fsync(descriptor)
            _require_audit_identity(parent_descriptor, path.name, descriptor)
            os.fsync(parent_descriptor)
            _require_audit_identity(parent_descriptor, path.name, descriptor)
        except BaseException:
            _rollback_audit_append(
                parent_descriptor,
                path.name,
                descriptor,
                original_size,
            )
            raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)
