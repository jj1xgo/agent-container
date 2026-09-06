from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import os  # noqa: F401 -- kept as a patch seam: tests patch os.chmod on this module
from pathlib import Path
import secrets
import shutil
import socket
import threading
from typing import Any

from agent_container.broker.artifacts import RuntimeArtifacts
from agent_container.broker.audit import AuditLog
from agent_container.broker.runtime import allocate_run_dir
from agent_container.broker.runtime import bind_private_listener
from agent_container.broker.runtime import create_private_file
from agent_container.broker.runtime import generate_capability
from agent_container.github_broker_error import BROKER_FAILURE_STAGES
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_broker_policy import validate_issue_number
from agent_container.github_broker_policy import validate_pr_number
from agent_container.github_broker_protocol import BrokerRequest
from agent_container.github_broker_protocol import PROTOCOL_VERSION
from agent_container.github_broker_protocol import MAX_REQUEST_NONCE
from agent_container.state import ensure_private_directory
from agent_container.state import github_broker_project_label


_LABEL = "broker"
_AUDIT_LABEL = "broker audit"
_AUDIT_STATUSES = frozenset({"ok", "denied", "error"})
_POLICY_VERSION = 1


@dataclass
class BrokerSession:
    policy: BrokerPolicy
    run_id: str
    run_dir: Path
    socket_path: Path
    capability_path: Path
    audit_file: Path
    _capability: str = field(repr=False)
    _artifacts: RuntimeArtifacts = field(repr=False)
    _seen_sequences: set[int] = field(default_factory=set, repr=False)
    _listener: socket.socket | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)
    _cleanup_complete: bool = field(default=False, repr=False)
    _lifecycle_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    @classmethod
    def create(cls, state_root: Path, policy: BrokerPolicy) -> "BrokerSession":
        root = ensure_private_directory(state_root)
        broker_root = ensure_private_directory(root / "github-broker", create=True)
        audit_root = ensure_private_directory(broker_root / "audit", create=True)
        run_root = ensure_private_directory(broker_root / "r", create=True)
        project_label = github_broker_project_label(policy.project_id)
        project_root = ensure_private_directory(
            run_root / project_label, create=True
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
        try:
            artifacts = RuntimeArtifacts.open(run_dir, label=_LABEL)
        except Exception:
            shutil.rmtree(run_dir)
            raise
        try:
            artifacts.track_file("capability")
        except Exception:
            artifacts.close()
            shutil.rmtree(run_dir)
            raise
        return cls(
            policy=policy,
            run_id=run_id,
            run_dir=run_dir,
            socket_path=run_dir / "broker.sock",
            capability_path=capability_path,
            audit_file=audit_file,
            _capability=capability,
            _artifacts=artifacts,
        )

    @property
    def run_label(self) -> str:
        return hashlib.sha256(self.run_id.encode("ascii")).hexdigest()[:16]

    def authorize(self, request: BrokerRequest) -> dict[str, Any]:
        with self._lifecycle_lock:
            if self._closed:
                raise ValueError("broker session is closed")
            if request.version != PROTOCOL_VERSION:
                raise ValueError("broker protocol version is not supported")
            if not secrets.compare_digest(request.capability, self._capability):
                raise ValueError("broker request is not authorized")
            if request.project_id != self.policy.project_id:
                raise ValueError("broker request project is not allowed")
            if (
                not 1 <= request.sequence <= MAX_REQUEST_NONCE
                or request.sequence in self._seen_sequences
                or len(self._seen_sequences) >= 4096
            ):
                raise ValueError("broker request sequence is invalid")
            operation = self.policy.validate_operation(request.operation)
            self._seen_sequences.add(request.sequence)
        return {"operation": operation, "payload": request.payload}

    def deactivate(self) -> None:
        with self._lifecycle_lock:
            self._closed = True
            self._capability = ""

    def open_listener(self, backlog: int = 4) -> socket.socket:
        if self._closed or self._listener is not None:
            raise ValueError("broker listener state is invalid")
        listener = bind_private_listener(
            self.socket_path, backlog=backlog, label=_LABEL
        )
        try:
            self._artifacts.track_socket("broker.sock")
        except Exception:
            listener.close()
            raise
        self._listener = listener
        return listener

    def audit(
        self,
        *,
        operation: str,
        status: str,
        ref: str | None = None,
        pr_number: int | None = None,
        issue_number: int | None = None,
        bytes_transferred: int = 0,
        stage: str | None = None,
    ) -> None:
        if self._closed:
            raise ValueError("broker session is closed")
        self.policy.validate_operation(operation)
        if status not in _AUDIT_STATUSES:
            raise ValueError("broker audit status is invalid")
        if status == "error":
            if stage not in BROKER_FAILURE_STAGES:
                raise ValueError("broker audit stage is invalid")
        elif stage is not None:
            raise ValueError("broker audit stage is invalid")
        if ref is not None:
            self.policy.validate_push_ref(ref)
        if pr_number is not None:
            validate_pr_number(pr_number)
        if issue_number is not None:
            validate_issue_number(issue_number)
        if (
            isinstance(bytes_transferred, bool)
            or not isinstance(bytes_transferred, int)
            or not 0 <= bytes_transferred <= 1 << 50
        ):
            raise ValueError("broker audit byte count is invalid")
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run": self.run_label,
            "project": self.policy.project_id,
            "repository": self.policy.repository.slug,
            "operation": operation,
            "status": status,
            "bytes": bytes_transferred,
            "policy_version": _POLICY_VERSION,
        }
        if ref is not None:
            record["ref"] = ref
        if pr_number is not None:
            record["pr_number"] = pr_number
        if issue_number is not None:
            record["issue_number"] = issue_number
        if stage is not None:
            record["stage"] = stage
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
        if self._artifacts.remove():
            cleanup_failed = True
        if cleanup_failed:
            raise ValueError("broker cleanup failed")
        self._cleanup_complete = True

    def __enter__(self) -> "BrokerSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
