"""Credential-free authorization and persistence for one family intake run."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Callable

from agent_container.family_intake_protocol import encode_request_frame
from agent_container.family_intake_protocol import FamilyIntakeRequest
from agent_container.family_intake_protocol import FamilyIntakeResponse
from agent_container.family_issue import canonicalize_family_issue
from agent_container.family_issue import parse_family_issue_draft
from agent_container.family_pending import create_pending
from agent_container.family_pending import PendingCapacityError
from agent_container.family_state import load_family_binding
from agent_container.state import validate_project_id


_CAPABILITY = re.compile(r"^[A-Za-z0-9_-]{43}$")
_MAX_PROCESS_STAT_BYTES = 8192
_MAX_PROCESS_DEPTH = 64
_ProcessIdentity = tuple[int, int, int]


class FamilyIntakeDenied(ValueError):
    def __init__(self) -> None:
        super().__init__("family intake request is not authorized")


class FamilyIntakeInternalError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("family intake persistence failed")


def _exact_nonnegative_integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("family intake session is invalid")
    return value


def _validate_store(store: object, project_id: str) -> Path:
    if not isinstance(store, Path) or not store.is_absolute():
        raise ValueError("family intake session is invalid")
    if (
        store.name != "pending"
        or store.parent.name != project_id
        or store.parent.parent.name != "projects"
    ):
        raise ValueError("family intake session is invalid")
    return store


def _read_process_identity(pid: int) -> _ProcessIdentity:
    if type(pid) is not int or pid <= 0:
        raise ValueError("family intake process identity is invalid")
    process_path = Path("/proc") / str(pid)
    stat_path = process_path / "stat"
    descriptor: int | None = None
    try:
        owner_uid = process_path.stat().st_uid
        descriptor = os.open(
            stat_path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        body = bytearray()
        while len(body) <= _MAX_PROCESS_STAT_BYTES:
            chunk = os.read(
                descriptor,
                min(1024, _MAX_PROCESS_STAT_BYTES + 1 - len(body)),
            )
            if not chunk:
                break
            body.extend(chunk)
        if len(body) > _MAX_PROCESS_STAT_BYTES:
            raise ValueError("family intake process identity is invalid")
        text = bytes(body).decode("ascii")
        opening = text.find("(")
        closing = text.rfind(")")
        if opening < 1 or closing <= opening or not text[closing + 1 :].startswith(" "):
            raise ValueError("family intake process identity is invalid")
        observed_pid = int(text[:opening].strip())
        fields = text[closing + 2 :].split()
        if observed_pid != pid or len(fields) < 20:
            raise ValueError("family intake process identity is invalid")
        parent_pid = int(fields[1])
        start_time = int(fields[19])
        if parent_pid < 0 or start_time <= 0 or owner_uid < 0:
            raise ValueError("family intake process identity is invalid")
        return parent_pid, start_time, owner_uid
    except (OSError, UnicodeDecodeError, ValueError):
        raise ValueError("family intake process identity is invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


@dataclass
class FamilyIntakeSession:
    """One expected runtime process and its single-use intake capability."""

    project_id: str
    capability: str = field(repr=False)
    expires_at: int
    consumed: bool = False
    store: Path = field(kw_only=True, repr=False)
    binding_path: Path = field(kw_only=True, repr=False)
    audit_path: Path = field(kw_only=True, repr=False)
    owner_uid: int = field(default_factory=os.getuid, kw_only=True)
    clock: Callable[[], int] = field(
        default=lambda: int(time.time()), kw_only=True, repr=False
    )
    random_bytes: Callable[[int], bytes] = field(
        default=os.urandom, kw_only=True, repr=False
    )
    process_reader: Callable[[int], _ProcessIdentity] = field(
        default=_read_process_identity, kw_only=True, repr=False
    )
    _active: bool = field(default=True, init=False, repr=False)
    _failed: bool = field(default=False, init=False, repr=False)
    _registration_attempted: bool = field(default=False, init=False, repr=False)
    _runtime_root: tuple[int, int] | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def __post_init__(self) -> None:
        try:
            self.project_id = validate_project_id(self.project_id)
        except (TypeError, ValueError):
            raise ValueError("family intake session is invalid") from None
        if (
            type(self.capability) is not str
            or _CAPABILITY.fullmatch(self.capability) is None
        ):
            raise ValueError("family intake session is invalid")
        self.expires_at = _exact_nonnegative_integer(self.expires_at)
        self.owner_uid = _exact_nonnegative_integer(self.owner_uid)
        if type(self.consumed) is not bool:
            raise ValueError("family intake session is invalid")
        self.store = _validate_store(self.store, self.project_id)
        if (
            not isinstance(self.binding_path, Path)
            or not self.binding_path.is_absolute()
            or self.binding_path != self.store.parent / "binding.json"
            or not isinstance(self.audit_path, Path)
            or not self.audit_path.is_absolute()
            or self.audit_path
            != self.store.parent / "audit" / "events.jsonl"
            or not callable(self.clock)
            or not callable(self.random_bytes)
            or not callable(self.process_reader)
        ):
            raise ValueError("family intake session is invalid")

    def owns_store(self, store: Path) -> bool:
        return isinstance(store, Path) and store == self.store

    @property
    def failed(self) -> bool:
        with self._lock:
            return self._failed

    def _fail_locked(self) -> None:
        self._failed = True
        self._active = False
        self.capability = ""

    def _read_process(self, pid: int) -> _ProcessIdentity:
        try:
            identity = self.process_reader(pid)
        except (OSError, ProcessLookupError, RuntimeError, TypeError, ValueError):
            raise FamilyIntakeDenied() from None
        if (
            type(identity) is not tuple
            or len(identity) != 3
            or any(type(value) is not int for value in identity)
            or identity[0] < 0
            or identity[1] <= 0
            or identity[2] < 0
        ):
            raise FamilyIntakeDenied()
        return identity

    def register_runtime(self, peer_pid: object) -> None:
        with self._lock:
            if (
                not self._active
                or self.consumed
                or self._registration_attempted
                or type(peer_pid) is not int
                or peer_pid <= 0
            ):
                raise ValueError("family intake runtime registration is invalid")
            self._registration_attempted = True
            first = self._read_process(peer_pid)
            second = self._read_process(peer_pid)
            if first != second or first[2] != self.owner_uid:
                raise ValueError("family intake runtime registration is invalid")
            self._runtime_root = (peer_pid, first[1])

    def validate_peer(self, peer_pid: object, peer_uid: object) -> None:
        with self._lock:
            root = self._runtime_root
            if (
                not self._active
                or root is None
                or type(peer_pid) is not int
                or type(peer_uid) is not int
                or peer_pid <= 0
                or peer_uid != self.owner_uid
            ):
                raise FamilyIntakeDenied()
            snapshots: dict[int, _ProcessIdentity] = {}
            current = peer_pid
            for _depth in range(_MAX_PROCESS_DEPTH):
                if current in snapshots:
                    raise FamilyIntakeDenied()
                identity = self._read_process(current)
                if identity[2] != self.owner_uid:
                    raise FamilyIntakeDenied()
                snapshots[current] = identity
                if current == root[0]:
                    if identity[1] != root[1]:
                        raise FamilyIntakeDenied()
                    break
                parent = identity[0]
                if parent <= 0 or parent == current:
                    raise FamilyIntakeDenied()
                current = parent
            else:
                raise FamilyIntakeDenied()
            for pid, expected in snapshots.items():
                if self._read_process(pid) != expected:
                    raise FamilyIntakeDenied()

    def handle(self, request: FamilyIntakeRequest) -> FamilyIntakeResponse:
        """Validate completely, then consume and persist under one session lock."""

        try:
            encode_request_frame(request)
            draft = parse_family_issue_draft(request.payload)
            issue = canonicalize_family_issue(draft)
        except (TypeError, ValueError):
            raise FamilyIntakeDenied() from None
        observed_now = self.clock()
        if type(observed_now) is not int or observed_now < 0:
            with self._lock:
                self._fail_locked()
            raise FamilyIntakeInternalError() from None

        with self._lock:
            if (
                not self._active
                or self._runtime_root is None
                or self.consumed
                or observed_now >= self.expires_at
                or request.version != 1
                or request.operation != "issue_create_request"
                or not secrets.compare_digest(request.capability, self.capability)
            ):
                raise FamilyIntakeDenied()
            try:
                load_family_binding(self.binding_path)
            except (OSError, TypeError, ValueError):
                raise FamilyIntakeDenied() from None

            # Fail closed from the first persistence attempt. create_pending can
            # have published durably before a later fsync reports ambiguity.
            self.consumed = True
            try:
                pending = create_pending(
                    self.store,
                    self.project_id,
                    issue,
                    now=observed_now,
                    audit_path=self.audit_path,
                    random_bytes=self.random_bytes,
                )
            except PendingCapacityError:
                raise FamilyIntakeDenied() from None
            except (OSError, RuntimeError, TypeError, ValueError):
                self._fail_locked()
                raise FamilyIntakeInternalError() from None
            return FamilyIntakeResponse(
                version=1,
                status="pending",
                request_id=pending.request_id,
                expires_at=pending.expires_at,
            )

    def deactivate(self) -> None:
        with self._lock:
            self._active = False
            self.capability = ""
