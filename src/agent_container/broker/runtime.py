"""Host-side broker runtime: private artifacts and the serve lifecycle."""

import os
from pathlib import Path
import secrets
import socket
import stat

from agent_container.broker.capability import CAPABILITY_PATTERN


MAX_UNIX_SOCKET_PATH_BYTES = 107
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def create_private_file(path: Path, body: str, *, label: str) -> None:
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
                raise OSError(f"{label} private file write failed")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def allocate_run_dir(
    project_root: Path, *, label: str, attempts: int = 8
) -> tuple[str, Path]:
    for _ in range(attempts):
        run_id = secrets.token_hex(8)
        run_dir = project_root / run_id
        try:
            run_dir.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return run_id, run_dir
    raise FileExistsError(f"could not allocate {label} runtime")


def generate_capability(*, label: str) -> str:
    capability = secrets.token_urlsafe(32)
    if CAPABILITY_PATTERN.fullmatch(capability) is None:
        raise RuntimeError(f"generated {label} capability has invalid format")
    return capability


def bind_private_listener(
    socket_path: Path, *, backlog: int, label: str
) -> socket.socket:
    if len(os.fsencode(socket_path)) > MAX_UNIX_SOCKET_PATH_BYTES:
        raise ValueError(f"{label} socket path is too long")
    if socket_path.exists() or socket_path.is_symlink():
        raise FileExistsError(f"{label} socket path already exists")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        listener.listen(backlog)
    except Exception:
        listener.close()
        try:
            metadata = socket_path.lstat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISSOCK(metadata.st_mode):
                socket_path.unlink()
        raise
    return listener


def remove_runtime_artifacts(
    *, capability_path: Path, socket_path: Path, run_dir: Path
) -> bool:
    failed = False
    for path, expected_type in (
        (capability_path, stat.S_ISREG),
        (socket_path, stat.S_ISSOCK),
    ):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            failed = True
            continue
        if not expected_type(metadata.st_mode):
            failed = True
            continue
        try:
            path.unlink()
        except OSError:
            failed = True
    try:
        run_dir.rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        failed = True
    return failed
