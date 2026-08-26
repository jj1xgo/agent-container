from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
from typing import Any, TextIO

from agent_container.github_broker_error import BROKER_FAILURE_STAGES
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_broker_policy import validate_pr_number
from agent_container.github_broker_protocol import BrokerRequest
from agent_container.github_broker_protocol import PROTOCOL_VERSION
from agent_container.github_broker_protocol import MAX_REQUEST_NONCE
from agent_container.state import ensure_private_directory
from agent_container.state import github_broker_project_label


_CAPABILITY = re.compile(r"^[A-Za-z0-9_-]{43}$")
_AUDIT_STATUSES = frozenset(
    {"ok", "denied", "error", "client-disconnected", "timeout"}
)
_POLICY_VERSION = 1
_MAX_UNIX_SOCKET_PATH_BYTES = 107


def _create_private_file(path: Path, body: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _open_audit_file(path: Path) -> TextIO:
    if path.is_symlink():
        raise ValueError("broker audit file must not be a symlink")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        os.close(descriptor)
        raise PermissionError("broker audit file must have mode 0600")
    if metadata.st_uid != os.getuid():
        os.close(descriptor)
        raise PermissionError("broker audit file must be owned by the current user")
    return os.fdopen(descriptor, "a", encoding="utf-8")


@dataclass
class BrokerSession:
    policy: BrokerPolicy
    run_id: str
    run_dir: Path
    socket_path: Path
    capability_path: Path
    audit_file: Path
    _capability: str = field(repr=False)
    _seen_sequences: set[int] = field(default_factory=set, repr=False)
    _listener: socket.socket | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)

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
        for _ in range(8):
            run_id = secrets.token_hex(8)
            run_dir = project_root / run_id
            try:
                run_dir.mkdir(mode=0o700)
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError("could not allocate broker runtime")
        capability = secrets.token_urlsafe(32)
        if _CAPABILITY.fullmatch(capability) is None:
            shutil.rmtree(run_dir)
            raise RuntimeError("generated broker capability has invalid format")
        capability_path = run_dir / "capability"
        try:
            _create_private_file(capability_path, capability + "\n")
        except Exception:
            shutil.rmtree(run_dir)
            raise
        return cls(
            policy=policy,
            run_id=run_id,
            run_dir=run_dir,
            socket_path=run_dir / "broker.sock",
            capability_path=capability_path,
            audit_file=audit_root / "events.jsonl",
            _capability=capability,
        )

    @property
    def run_label(self) -> str:
        return hashlib.sha256(self.run_id.encode("ascii")).hexdigest()[:16]

    def authorize(self, request: BrokerRequest) -> dict[str, Any]:
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

    def open_listener(self, backlog: int = 4) -> socket.socket:
        if self._closed or self._listener is not None:
            raise ValueError("broker listener state is invalid")
        if len(os.fsencode(self.socket_path)) > _MAX_UNIX_SOCKET_PATH_BYTES:
            raise ValueError("broker socket path is too long")
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise FileExistsError("broker socket path already exists")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            listener.listen(backlog)
        except Exception:
            listener.close()
            if self.socket_path.exists() and stat.S_ISSOCK(
                self.socket_path.lstat().st_mode
            ):
                self.socket_path.unlink()
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
        if stage is not None:
            record["stage"] = stage
        with _open_audit_file(self.audit_file) as stream:
            json.dump(record, stream, ensure_ascii=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._capability = ""
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        for path in (self.socket_path, self.capability_path):
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if path == self.socket_path and not stat.S_ISSOCK(metadata.st_mode):
                raise ValueError("broker socket path changed during cleanup")
            if path == self.capability_path and not stat.S_ISREG(metadata.st_mode):
                raise ValueError("broker capability path changed during cleanup")
            path.unlink()
        self.run_dir.rmdir()

    def __enter__(self) -> "BrokerSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
