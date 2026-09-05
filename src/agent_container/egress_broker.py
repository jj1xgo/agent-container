from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import socket
import threading

from agent_container.broker.audit import AuditLog
from agent_container.broker.runtime import allocate_run_dir
from agent_container.broker.runtime import bind_private_listener
from agent_container.broker.runtime import create_private_file
from agent_container.broker.runtime import generate_capability
from agent_container.broker.runtime import remove_runtime_artifacts
from agent_container.egress_broker_protocol import EgressRequest
from agent_container.egress_broker_protocol import PROTOCOL_VERSION
from agent_container.egress_policy import EgressPolicy
from agent_container.state import ensure_private_directory
from agent_container.state import StateLayout
from agent_container.state import validate_agent


MANAGED_EGRESS_DOMAINS: dict[str, frozenset[str]] = {
    "codex": frozenset(),
    "claude": frozenset(),
}
_LABEL = "egress broker"
_AUDIT_LABEL = "egress broker audit"
# The container-side adapter reads the capability through a read-only bind
# mount and rejects writable files, so the file is created owner-read-only.
_CAPABILITY_FILE_MODE = 0o400
_AUDIT_STATUSES = frozenset({"ok", "denied", "error"})
_AUDIT_STAGES = frozenset(
    {"authentication", "policy", "resolve", "connect", "limit", "relay", "unavailable"}
)
_MAX_TUNNEL_BYTES = 1 << 31


class EgressAuthorizationError(ValueError):
    def __init__(self, stage: str) -> None:
        if stage not in {"authentication", "policy"}:
            raise ValueError("egress authorization stage is invalid")
        self.stage = stage
        super().__init__("egress broker request is not allowed")


@dataclass
class EgressBrokerSession:
    project_id: str
    agent: str
    owner_uid: int
    run_id: str
    run_dir: Path
    socket_path: Path
    capability_path: Path
    audit_file: Path
    _allowed_domains: frozenset[str] = field(repr=False)
    _capability: str = field(repr=False)
    _expected_sequence: int = field(default=1, repr=False)
    _listener: socket.socket | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)
    _cleanup_complete: bool = field(default=False, repr=False)
    _lifecycle_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    @classmethod
    def create(
        cls, layout: StateLayout, agent: str, policy: EgressPolicy
    ) -> "EgressBrokerSession":
        ensure_private_directory(layout.root)
        selected_agent = validate_agent(agent)
        if policy.version != 1 or policy.mode != "allowlist":
            raise ValueError("egress policy is invalid")
        allowed = frozenset(policy.additional_domains) | MANAGED_EGRESS_DOMAINS[
            selected_agent
        ]
        ensure_private_directory(layout.egress_broker_root, create=True)
        ensure_private_directory(layout.egress_broker_root / "audit", create=True)
        ensure_private_directory(layout.egress_broker_root / "r", create=True)
        project_root = ensure_private_directory(
            layout.egress_broker_run_root, create=True
        )
        audit_file = layout.egress_broker_audit_file
        AuditLog(audit_file, label=_AUDIT_LABEL).validate()

        run_id, run_dir = allocate_run_dir(project_root, label=_LABEL)
        try:
            capability = generate_capability(label=_LABEL)
        except RuntimeError:
            shutil.rmtree(run_dir)
            raise
        capability_path = run_dir / "capability"
        try:
            create_private_file(
                capability_path,
                capability + "\n",
                label=_LABEL,
                mode=_CAPABILITY_FILE_MODE,
            )
        except Exception:
            shutil.rmtree(run_dir)
            raise
        return cls(
            project_id=layout.project_id,
            agent=selected_agent,
            owner_uid=os.getuid(),
            run_id=run_id,
            run_dir=run_dir,
            socket_path=run_dir / "broker.sock",
            capability_path=capability_path,
            audit_file=audit_file,
            _allowed_domains=allowed,
            _capability=capability,
        )

    @property
    def run_label(self) -> str:
        return hashlib.sha256(self.run_id.encode("ascii")).hexdigest()[:16]

    def authorize(self, request: EgressRequest, peer_uid: int) -> str:
        with self._lifecycle_lock:
            if self._closed:
                raise ValueError("egress broker session is closed")
            if peer_uid != self.owner_uid:
                raise ValueError("egress broker request is not authorized")
            if request.version != PROTOCOL_VERSION:
                raise ValueError("egress broker protocol version is not supported")
            if not secrets.compare_digest(request.capability, self._capability):
                raise ValueError("egress broker request is not authorized")
            if request.project_id != self.project_id:
                raise ValueError("egress broker request project is not allowed")
            if request.sequence != self._expected_sequence:
                raise ValueError("egress broker request sequence is invalid")
            if request.operation != "connect" or request.port != 443:
                raise EgressAuthorizationError("policy")
            if request.domain not in self._allowed_domains:
                raise EgressAuthorizationError("policy")
            self._expected_sequence += 1
            return request.domain

    def deactivate(self) -> None:
        with self._lifecycle_lock:
            self._closed = True
            self._capability = ""

    def open_listener(self, backlog: int = 4) -> socket.socket:
        if self._closed or self._listener is not None:
            raise ValueError("egress broker listener state is invalid")
        listener = bind_private_listener(
            self.socket_path, backlog=backlog, label=_LABEL
        )
        self._listener = listener
        return listener

    def audit(
        self,
        status: str,
        *,
        stage: str | None = None,
        bytes_from_client: int | None = None,
        bytes_from_upstream: int | None = None,
    ) -> None:
        if self._closed:
            raise ValueError("egress broker session is closed")
        if status not in _AUDIT_STATUSES:
            raise ValueError("egress broker audit status is invalid")
        if status == "ok":
            if stage is not None:
                raise ValueError("egress broker audit stage is invalid")
        elif stage not in _AUDIT_STAGES:
            raise ValueError("egress broker audit stage is invalid")
        counts = {
            "bytes_from_client": bytes_from_client,
            "bytes_from_upstream": bytes_from_upstream,
        }
        for value in counts.values():
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _MAX_TUNNEL_BYTES
            ):
                raise ValueError("egress broker audit byte count is invalid")
        record: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run": self.run_label,
            "project": self.project_id,
            "agent": self.agent,
            "operation": "connect",
            "status": status,
        }
        if stage is not None:
            record["stage"] = stage
        for key, value in counts.items():
            if value is not None:
                record[key] = value
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
            raise ValueError("egress broker cleanup failed")
        self._cleanup_complete = True

    def __enter__(self) -> "EgressBrokerSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
