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


class FamilyRuntimeError(Exception):
    pass


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
            try:
                os.close(descriptor)
            except BaseException:
                descriptor = -1
                try:
                    os.close(child)
                except OSError:
                    pass
                raise
            descriptor = child
        return descriptor
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _snapshot_descriptor(
    descriptor: int,
) -> tuple[tuple[int, int], tuple[int, int]]:
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


def _snapshot(path: Path) -> tuple[tuple[int, int], tuple[int, int]]:
    descriptor = _open_directory(path)
    try:
        return _snapshot_descriptor(descriptor)
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
    _directory_descriptor: int = field(default=-1, repr=False)

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
        descriptor = _open_directory(socket_dir)
        try:
            directory_identity, socket_identity = _snapshot_descriptor(descriptor)
            return cls(
                socket_dir,
                capability,
                environment,
                directory_identity,
                socket_identity,
                descriptor,
            )
        except BaseException:
            os.close(descriptor)
            raise

    def revalidate(self) -> None:
        if (
            self._directory_identity is None
            or self._socket_identity is None
            or self._directory_descriptor < 0
        ):
            raise ValueError("family runtime mount is invalid")
        try:
            pinned = _snapshot_descriptor(self._directory_descriptor)
            observed = _snapshot(self.socket_dir)
        except (OSError, ValueError):
            raise ValueError("family runtime mount is invalid") from None
        expected = (self._directory_identity, self._socket_identity)
        if pinned != expected or observed != expected:
            raise ValueError("family runtime mount is invalid")

    @property
    def pass_fds(self) -> tuple[int, ...]:
        if self._directory_descriptor < 0:
            raise ValueError("family runtime mount is invalid")
        return (self._directory_descriptor,)

    @property
    def mount_source(self) -> Path:
        return Path(f"/proc/self/fd/{self.pass_fds[0]}")

    def close(self) -> None:
        descriptor = self._directory_descriptor
        if descriptor >= 0:
            os.close(descriptor)
            object.__setattr__(self, "_directory_descriptor", -1)

    @property
    def socket_path(self) -> Path:
        return self.socket_dir / "intake.sock"

    @property
    def container_name(self) -> str:
        return f"agent-family-{self.socket_dir.parent.name}-{self.socket_dir.name}"
