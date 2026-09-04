"""Container-side validation of broker runtime paths and capabilities."""

import os
from pathlib import Path
import re
import socket
import stat
from typing import Any
from typing import Callable


CAPABILITY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CAPABILITY_FILE_BYTES = 44
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", None)


def validate_exact_path(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{label} is invalid")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(f"{label} is invalid") from None
    if resolved != path:
        raise ValueError(f"{label} is invalid")
    return resolved


def read_capability(path: Path, *, label: str) -> str:
    if not path.is_absolute() or _NONBLOCK is None:
        raise ValueError(f"{label} is invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
    except OSError:
        raise ValueError(f"{label} is invalid") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_size != _CAPABILITY_FILE_BYTES
        ):
            raise ValueError(f"{label} is invalid")

        output = bytearray()
        while len(output) < _CAPABILITY_FILE_BYTES + 1:
            chunk = os.read(descriptor, _CAPABILITY_FILE_BYTES + 1 - len(output))
            if not chunk:
                break
            output.extend(chunk)
        body = bytes(output)

        try:
            resolved = path.resolve(strict=True)
            path_metadata = path.lstat()
        except (OSError, RuntimeError):
            raise ValueError(f"{label} is invalid") from None
        if (
            resolved != path
            or metadata.st_dev != path_metadata.st_dev
            or metadata.st_ino != path_metadata.st_ino
        ):
            raise ValueError(f"{label} is invalid")
    except OSError:
        raise ValueError(f"{label} is invalid") from None
    finally:
        os.close(descriptor)
    try:
        capability = body.decode("ascii").removesuffix("\n")
    except UnicodeDecodeError:
        raise ValueError(f"{label} is invalid") from None
    if (
        CAPABILITY_PATTERN.fullmatch(capability) is None
        or body != (capability + "\n").encode("ascii")
    ):
        raise ValueError(f"{label} is invalid")
    return capability


def validate_socket(path: Path, *, label: str) -> Path:
    metadata = path.stat()
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
    ):
        raise ValueError(f"{label} is invalid")
    return path


def connect_unix(
    path: Path,
    *,
    timeout: float,
    socket_factory: Callable[..., Any] = socket.socket,
) -> Any:
    client = socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(timeout)
        client.connect(str(path))
    except BaseException:
        client.close()
        raise
    return client
