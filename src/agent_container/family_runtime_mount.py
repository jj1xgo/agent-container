"""Credential-neutral, identity-pinned family runtime mount contract."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Mapping


_CAPABILITY = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CONTAINER_SOCKET = "/run/agent-family/intake.sock"
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _open_directory(path: Path) -> int:
    descriptor = os.open("/", os.O_RDONLY | _DIRECTORY | _CLOEXEC)
    try:
        for component in path.parts[1:]:
            child = os.open(
                component,
                os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _snapshot(path: Path) -> tuple[tuple[int, int], tuple[int, int]]:
    descriptor = _open_directory(path)
    try:
        directory = os.fstat(descriptor)
        socket_metadata = os.stat(
            "intake.sock", dir_fd=descriptor, follow_symlinks=False
        )
        if (
            not stat.S_ISDIR(directory.st_mode)
            or stat.S_IMODE(directory.st_mode) != 0o700
            or directory.st_uid != os.getuid()
            or not stat.S_ISSOCK(socket_metadata.st_mode)
            or stat.S_IMODE(socket_metadata.st_mode) != 0o600
            or socket_metadata.st_uid != os.getuid()
        ):
            raise ValueError("family runtime mount is invalid")
        return _identity(directory), _identity(socket_metadata)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class FamilyRuntimeMount:
    socket_dir: Path
    capability: str = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)
    _directory_identity: tuple[int, int] | None = field(
        default=None, repr=False
    )
    _socket_identity: tuple[int, int] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.socket_dir, Path) or not self.socket_dir.is_absolute():
            raise ValueError("family runtime mount is invalid")
        if type(self.capability) is not str or _CAPABILITY.fullmatch(self.capability) is None:
            raise ValueError("family runtime mount is invalid")
        expected = {
            "AGENT_FAMILY_SOCKET": _CONTAINER_SOCKET,
            "AGENT_FAMILY_CAPABILITY": self.capability,
        }
        if dict(self.environment) != expected:
            raise ValueError("family runtime mount is invalid")
        object.__setattr__(self, "environment", MappingProxyType(expected))

    @classmethod
    def capture(
        cls, socket_dir: Path, capability: str, environment: Mapping[str, str]
    ) -> "FamilyRuntimeMount":
        directory_identity, socket_identity = _snapshot(socket_dir)
        return cls(
            socket_dir,
            capability,
            environment,
            directory_identity,
            socket_identity,
        )

    def revalidate(self) -> None:
        if self._directory_identity is None or self._socket_identity is None:
            raise ValueError("family runtime mount is invalid")
        try:
            observed = _snapshot(self.socket_dir)
        except (OSError, ValueError):
            raise ValueError("family runtime mount is invalid") from None
        if observed != (self._directory_identity, self._socket_identity):
            raise ValueError("family runtime mount is invalid")

    @property
    def socket_path(self) -> Path:
        return self.socket_dir / "intake.sock"

    @property
    def container_name(self) -> str:
        return f"agent-family-{self.socket_dir.parent.name}-{self.socket_dir.name}"
