from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import socket
import threading
from typing import Iterator

from agent_container.broker.audit import AuditLog
from agent_container.broker.runtime import allocate_run_dir
from agent_container.broker.runtime import bind_private_listener
from agent_container.broker.runtime import create_private_file
from agent_container.broker.runtime import generate_capability
from agent_container.broker.runtime import remove_runtime_artifacts
from agent_container.handover_broker_protocol import HandoverRequest
from agent_container.handover_broker_protocol import PROTOCOL_VERSION
from agent_container.handover_writer import validate_handover_content
from agent_container.state import ensure_private_directory
from agent_container.state import handover_broker_project_label
from agent_container.state import validate_project_id


_LABEL = "handover broker"
_AUDIT_LABEL = "handover broker audit"
_HANDOVER_FILENAME = re.compile(
    r"^\d{4}-\d{2}-\d{2}_\d{6}_[0-9a-f]{8}\.md$"
)
_AUDIT_STATUSES = frozenset({"ok", "denied", "error"})
_AUDIT_STAGES = frozenset(
    {
        "authentication",
        "schema",
        "size",
        "content-policy",
        "filesystem-boundary",
        "write",
        "unavailable",
        "response",
    }
)


def _validate_project_directory(project_dir: Path, project_id: str) -> Path:
    if not project_dir.is_absolute() or project_dir.name != project_id:
        raise ValueError("handover project directory is invalid")
    try:
        resolved = project_dir.resolve(strict=True)
    except OSError:
        raise ValueError("handover project directory is invalid") from None
    if resolved != project_dir or project_dir.is_symlink() or not project_dir.is_dir():
        raise ValueError("handover project directory is invalid")
    return resolved


def _validate_container_path(path: str, project_id: str) -> str:
    if not isinstance(path, str) or "\x00" in path or "\n" in path or "\r" in path:
        raise ValueError("handover broker audit path is invalid")
    parsed = PurePosixPath(path)
    expected_parent = PurePosixPath("/handovers") / project_id
    if (
        not parsed.is_absolute()
        or parsed.parent != expected_parent
        or _HANDOVER_FILENAME.fullmatch(parsed.name) is None
    ):
        raise ValueError("handover broker audit path is invalid")
    return path


@dataclass
class HandoverBrokerSession:
    project_id: str
    project_dir: Path
    owner_uid: int
    run_id: str
    run_dir: Path
    socket_path: Path
    capability_path: Path
    audit_file: Path
    _capability: str = field(repr=False)
    _listener: socket.socket | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)
    _cleanup_complete: bool = field(default=False, repr=False)
    _lifecycle_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )

    @classmethod
    def create(
        cls,
        state_root: Path,
        project_id: str,
        project_dir: Path,
    ) -> "HandoverBrokerSession":
        root = ensure_private_directory(state_root)
        validated_project = validate_project_id(project_id)
        bound_project_dir = _validate_project_directory(
            project_dir,
            validated_project,
        )
        broker_root = ensure_private_directory(root / "handover-broker", create=True)
        audit_root = ensure_private_directory(broker_root / "audit", create=True)
        run_root = ensure_private_directory(broker_root / "r", create=True)
        project_root = ensure_private_directory(
            run_root / handover_broker_project_label(validated_project),
            create=True,
        )
        audit_file = audit_root / "events.jsonl"
        AuditLog(audit_file, label=_AUDIT_LABEL).validate()

        run_id, run_dir = allocate_run_dir(project_root, label=_LABEL)
        try:
            capability = generate_capability(label=_LABEL)
        except RuntimeError:
            shutil.rmtree(run_dir)
            raise
        capability_path = run_dir / "capability"
        try:
            create_private_file(capability_path, capability + "\n", label=_LABEL)
        except Exception:
            shutil.rmtree(run_dir)
            raise
        return cls(
            project_id=validated_project,
            project_dir=bound_project_dir,
            owner_uid=os.getuid(),
            run_id=run_id,
            run_dir=run_dir,
            socket_path=run_dir / "broker.sock",
            capability_path=capability_path,
            audit_file=audit_file,
            _capability=capability,
        )

    @property
    def run_label(self) -> str:
        return hashlib.sha256(self.run_id.encode("ascii")).hexdigest()[:16]

    def authorize(
        self,
        request: HandoverRequest,
        peer_uid: int,
    ) -> tuple[str, str]:
        with self._lifecycle_lock:
            if self._closed:
                raise ValueError("handover broker session is closed")
            if peer_uid != self.owner_uid:
                raise ValueError("handover broker request is not authorized")
            if request.version != PROTOCOL_VERSION:
                raise ValueError("handover broker protocol version is not supported")
            if not secrets.compare_digest(request.capability, self._capability):
                raise ValueError("handover broker request is not authorized")
            if request.project_id != self.project_id:
                raise ValueError("handover broker request project is not allowed")
            if request.operation != "create":
                raise ValueError("handover broker request operation is not allowed")
        return validate_handover_content(request.title, request.body)

    @contextmanager
    def publication_guard(self) -> Iterator[None]:
        with self._lifecycle_lock:
            if self._closed:
                raise OSError("handover publication is unavailable")
            yield

    def deactivate(self) -> None:
        with self._lifecycle_lock:
            self._closed = True
            self._capability = ""

    def open_listener(self, backlog: int = 4) -> socket.socket:
        if self._closed or self._listener is not None:
            raise ValueError("handover broker listener state is invalid")
        listener = bind_private_listener(
            self.socket_path, backlog=backlog, label=_LABEL
        )
        self._listener = listener
        return listener

    def audit(self, status: str, *, stage: str, path: str = "") -> None:
        if self._closed:
            raise ValueError("handover broker session is closed")
        if status not in _AUDIT_STATUSES:
            raise ValueError("handover broker audit status is invalid")
        if stage not in _AUDIT_STAGES:
            raise ValueError("handover broker audit stage is invalid")
        if status == "ok":
            path = _validate_container_path(path, self.project_id)
        elif path:
            raise ValueError("handover broker audit path is invalid")

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run": self.run_label,
            "project": self.project_id,
            "operation": "create",
            "status": status,
            "stage": stage,
        }
        if path:
            record["path"] = path
        AuditLog(self.audit_file, label=_AUDIT_LABEL).append(record)

    def close(self) -> None:
        if self._cleanup_complete:
            return
        self.deactivate()
        cleanup_failed = False
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                cleanup_failed = True
            else:
                self._listener = None
        if remove_runtime_artifacts(
            capability_path=self.capability_path,
            socket_path=self.socket_path,
            run_dir=self.run_dir,
        ):
            cleanup_failed = True
        if cleanup_failed:
            raise ValueError("handover broker cleanup failed")
        self._cleanup_complete = True

    def __enter__(self) -> "HandoverBrokerSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
