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
import threading

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
_CAPABILITY = re.compile(r"^[A-Za-z0-9_-]{43}$")
_AUDIT_STATUSES = frozenset({"ok", "denied", "error"})
_AUDIT_STAGES = frozenset(
    {"authentication", "policy", "resolve", "connect", "limit", "relay", "unavailable"}
)
_MAX_TUNNEL_BYTES = 1 << 31
_MAX_UNIX_SOCKET_PATH_BYTES = 107
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class EgressAuthorizationError(ValueError):
    def __init__(self, stage: str) -> None:
        if stage not in {"authentication", "policy"}:
            raise ValueError("egress authorization stage is invalid")
        self.stage = stage
        super().__init__("egress broker request is not allowed")


def _create_private_file(path: Path, body: str) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
        0o400,
    )
    try:
        encoded = body.encode("ascii")
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("egress broker private file write failed")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_audit_file(path: Path) -> int:
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW | _NONBLOCK,
            0o600,
        )
    except OSError:
        raise ValueError(
            "egress broker audit file must be a regular non-symlink file"
        ) from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(
                "egress broker audit file must be a regular non-symlink file"
            )
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError("egress broker audit file must have mode 0600")
        if metadata.st_uid != os.getuid():
            raise PermissionError(
                "egress broker audit file must be owned by the current user"
            )
        try:
            current = os.stat(path, follow_symlinks=False)
        except OSError:
            raise ValueError(
                "egress broker audit file must be a regular non-symlink file"
            ) from None
        if current.st_dev != metadata.st_dev or current.st_ino != metadata.st_ino:
            raise ValueError("egress broker audit file changed during validation")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _write_audit_record(path: Path, record: dict[str, object]) -> None:
    body = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )
    descriptor = _open_audit_file(path)
    try:
        offset = 0
        while offset < len(body):
            written = os.write(descriptor, body[offset:])
            if written <= 0:
                raise OSError("egress broker audit write failed")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
        audit_descriptor = _open_audit_file(audit_file)
        os.close(audit_descriptor)

        for _ in range(8):
            run_id = secrets.token_hex(8)
            run_dir = project_root / run_id
            try:
                run_dir.mkdir(mode=0o700)
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError("could not allocate egress broker runtime")

        capability = secrets.token_urlsafe(32)
        if _CAPABILITY.fullmatch(capability) is None:
            shutil.rmtree(run_dir)
            raise RuntimeError("generated egress broker capability has invalid format")
        capability_path = run_dir / "capability"
        try:
            _create_private_file(capability_path, capability + "\n")
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
        if len(os.fsencode(self.socket_path)) > _MAX_UNIX_SOCKET_PATH_BYTES:
            raise ValueError("egress broker socket path is too long")
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise FileExistsError("egress broker socket path already exists")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            listener.listen(backlog)
        except Exception:
            listener.close()
            try:
                metadata = self.socket_path.lstat()
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISSOCK(metadata.st_mode):
                    self.socket_path.unlink()
            raise
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
        _write_audit_record(self.audit_file, record)

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
        for path, expected_type in (
            (self.capability_path, stat.S_ISREG),
            (self.socket_path, stat.S_ISSOCK),
        ):
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                cleanup_failed = True
                continue
            if not expected_type(metadata.st_mode):
                cleanup_failed = True
                continue
            try:
                path.unlink()
            except OSError:
                cleanup_failed = True
        try:
            self.run_dir.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            cleanup_failed = True
        if cleanup_failed:
            raise ValueError("egress broker cleanup failed")
        self._cleanup_complete = True

    def __enter__(self) -> "EgressBrokerSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
