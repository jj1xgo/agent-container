"""Identity-checked removal of a broker run directory and its artifacts."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
from typing import Callable


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | os.O_DIRECTORY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
Identity = tuple[int, int]


@dataclass(frozen=True)
class _Tracked:
    name: str
    expected_type: Callable[[int], bool]
    identity: Identity | None


@dataclass
class RuntimeArtifacts:
    run_dir: Path
    label: str
    _descriptor: int = field(default=-1, repr=False)
    _parent_descriptor: int = field(default=-1, repr=False)
    _name: str = field(default="", repr=False)
    _directory_identity: Identity | None = field(default=None, repr=False)
    _files: list[_Tracked] = field(default_factory=list, repr=False)
    _sockets: list[_Tracked] = field(default_factory=list, repr=False)
    _removed: bool = field(default=False, repr=False)

    @classmethod
    def open(cls, run_dir: Path, *, label: str) -> "RuntimeArtifacts":
        parent = os.open(run_dir.parent, _DIRECTORY_FLAGS)
        try:
            descriptor = os.open(run_dir.name, _DIRECTORY_FLAGS, dir_fd=parent)
        except BaseException:
            os.close(parent)
            raise
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid != os.getuid()
            ):
                raise PermissionError(f"{label} run directory is not private")
        except BaseException:
            os.close(descriptor)
            os.close(parent)
            raise
        return cls(
            run_dir=run_dir,
            label=label,
            _descriptor=descriptor,
            _parent_descriptor=parent,
            _name=run_dir.name,
            _directory_identity=(metadata.st_dev, metadata.st_ino),
        )

    def _require_open(self) -> None:
        if self._descriptor < 0:
            raise ValueError(f"{self.label} run directory is closed")

    def _track(
        self, name: str, expected_type: Callable[[int], bool], into: list[_Tracked]
    ) -> None:
        self._require_open()
        try:
            metadata = os.stat(name, dir_fd=self._descriptor, follow_symlinks=False)
        except FileNotFoundError:
            identity: Identity | None = None
        else:
            if not expected_type(metadata.st_mode):
                raise ValueError(f"{self.label} artifact type is invalid")
            identity = (metadata.st_dev, metadata.st_ino)
        into.append(_Tracked(name, expected_type, identity))

    def track_file(self, name: str) -> None:
        self._track(name, stat.S_ISREG, self._files)

    def track_socket(self, name: str) -> None:
        self._track(name, stat.S_ISSOCK, self._sockets)

    def _remove_entry(self, tracked: _Tracked) -> bool:
        try:
            metadata = os.stat(
                tracked.name, dir_fd=self._descriptor, follow_symlinks=False
            )
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if (
            tracked.identity is None
            or not tracked.expected_type(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != tracked.identity
        ):
            return False
        try:
            os.unlink(tracked.name, dir_fd=self._descriptor)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def _remove_directory(self) -> bool:
        try:
            current = os.stat(
                self._name, dir_fd=self._parent_descriptor, follow_symlinks=False
            )
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != self._directory_identity
            or stat.S_IMODE(current.st_mode) != 0o700
            or current.st_uid != os.getuid()
        ):
            return False
        try:
            os.rmdir(self._name, dir_fd=self._parent_descriptor)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def remove(self) -> bool:
        """Remove tracked files, then sockets, then the directory. True means failed.

        A failed removal keeps the descriptor so the caller can retry once the
        foreign entry has been dealt with; a successful one closes it.
        """
        if self._removed:
            return False
        self._require_open()
        failed = False
        for tracked in (*self._files, *self._sockets):
            if not self._remove_entry(tracked):
                failed = True
        if not self._remove_directory():
            failed = True
        if failed:
            return True
        self._removed = True
        self.close()
        return False

    def close(self) -> None:
        if self._descriptor >= 0:
            descriptor = self._descriptor
            self._descriptor = -1
            os.close(descriptor)
        if self._parent_descriptor >= 0:
            parent = self._parent_descriptor
            self._parent_descriptor = -1
            os.close(parent)
