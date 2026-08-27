from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import socket
import stat
import threading
from typing import Iterator

from agent_container.handover_broker_protocol import HandoverRequest
from agent_container.handover_broker_protocol import PROTOCOL_VERSION
from agent_container.handover_writer import validate_handover_content
from agent_container.state import ensure_private_directory
from agent_container.state import handover_broker_project_label
from agent_container.state import validate_project_id


_CAPABILITY = re.compile(r"^[A-Za-z0-9_-]{43}$")
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
_MAX_UNIX_SOCKET_PATH_BYTES = 107
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def _create_private_file(path: Path, body: str) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        encoded = body.encode("ascii")
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("handover broker private file write failed")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_audit_file(path: Path) -> int:
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | _NOFOLLOW
            | _NONBLOCK,
            0o600,
        )
    except OSError:
        raise ValueError(
            "handover broker audit file must be a regular non-symlink file"
        ) from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(
                "handover broker audit file must be a regular non-symlink file"
            )
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError(
                "handover broker audit file must have mode 0600"
            )
        if metadata.st_uid != os.getuid():
            raise PermissionError(
                "handover broker audit file must be owned by the current user"
            )
        try:
            current = os.stat(path, follow_symlinks=False)
        except OSError:
            raise ValueError(
                "handover broker audit file must be a regular non-symlink file"
            ) from None
        if (
            current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
        ):
            raise ValueError("handover broker audit file changed during validation")
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    return descriptor


def _write_audit_record(path: Path, record: dict[str, str]) -> None:
    body = (
        json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
    ).encode("ascii")
    descriptor = _open_audit_file(path)
    try:
        offset = 0
        while offset < len(body):
            written = os.write(descriptor, body[offset:])
            if written <= 0:
                raise OSError("handover broker audit write failed")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
            raise FileExistsError("could not allocate handover broker runtime")

        capability = secrets.token_urlsafe(32)
        if _CAPABILITY.fullmatch(capability) is None:
            shutil.rmtree(run_dir)
            raise RuntimeError("generated handover broker capability has invalid format")
        capability_path = run_dir / "capability"
        try:
            _create_private_file(capability_path, capability + "\n")
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
        if len(os.fsencode(self.socket_path)) > _MAX_UNIX_SOCKET_PATH_BYTES:
            raise ValueError("handover broker socket path is too long")
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise FileExistsError("handover broker socket path already exists")
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
            raise ValueError("handover broker cleanup failed")
        self._cleanup_complete = True

    def __enter__(self) -> "HandoverBrokerSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
