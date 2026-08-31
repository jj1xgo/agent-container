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
from agent_container.family_pending import append_family_audit
from agent_container.family_pending import create_pending
from agent_container.family_state import load_family_binding
from agent_container.state import validate_project_id


_CAPABILITY = re.compile(r"^[A-Za-z0-9_-]{43}$")


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


@dataclass
class FamilyIntakeSession:
    """One expected runtime process and its single-use intake capability."""

    project_id: str
    capability: str = field(repr=False)
    expires_at: int
    peer_pid: int
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
    _active: bool = field(default=True, init=False, repr=False)
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
        self.peer_pid = _exact_nonnegative_integer(self.peer_pid)
        self.owner_uid = _exact_nonnegative_integer(self.owner_uid)
        if self.peer_pid == 0 or type(self.consumed) is not bool:
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
        ):
            raise ValueError("family intake session is invalid")

    def owns_store(self, store: Path) -> bool:
        return isinstance(store, Path) and store == self.store

    def validate_peer(self, peer_pid: object, peer_uid: object) -> None:
        if (
            type(peer_pid) is not int
            or type(peer_uid) is not int
            or peer_pid != self.peer_pid
            or peer_uid != self.owner_uid
        ):
            raise ValueError("family intake request is not authorized")

    def handle(self, request: FamilyIntakeRequest) -> FamilyIntakeResponse:
        """Validate completely, then consume and persist under one session lock."""

        encode_request_frame(request)
        draft = parse_family_issue_draft(request.payload)
        issue = canonicalize_family_issue(draft)
        observed_now = self.clock()
        if type(observed_now) is not int or observed_now < 0:
            raise ValueError("family intake session is invalid")

        with self._lock:
            if (
                not self._active
                or self.consumed
                or observed_now >= self.expires_at
                or request.version != 1
                or request.operation != "issue_create_request"
                or not secrets.compare_digest(request.capability, self.capability)
            ):
                raise ValueError("family intake request is not authorized")
            load_family_binding(self.binding_path)

            # Fail closed from the first persistence attempt. create_pending can
            # have published durably before a later fsync reports ambiguity.
            self.consumed = True
            pending = create_pending(
                self.store,
                self.project_id,
                issue,
                now=observed_now,
                random_bytes=self.random_bytes,
            )
            append_family_audit(
                self.audit_path,
                timestamp=observed_now,
                project_id=self.project_id,
                request_id=pending.request_id,
                operation="intake",
                status="pending",
                stage="intake",
            )
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
