"""Private append-only audit log shared by every broker."""

import json
import os
from pathlib import Path
import stat
from typing import Mapping


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class AuditLog:
    def __init__(self, path: Path, *, label: str) -> None:
        self.path = path
        self.label = label

    def open_descriptor(self) -> int:
        try:
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW | _NONBLOCK,
                0o600,
            )
        except OSError:
            raise ValueError(
                f"{self.label} file must be a regular non-symlink file"
            ) from None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(
                    f"{self.label} file must be a regular non-symlink file"
                )
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise PermissionError(f"{self.label} file must have mode 0600")
            if metadata.st_uid != os.getuid():
                raise PermissionError(
                    f"{self.label} file must be owned by the current user"
                )
            try:
                current = os.stat(self.path, follow_symlinks=False)
            except OSError:
                raise ValueError(
                    f"{self.label} file must be a regular non-symlink file"
                ) from None
            if current.st_dev != metadata.st_dev or current.st_ino != metadata.st_ino:
                raise ValueError(f"{self.label} file changed during validation")
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        return descriptor

    def validate(self) -> None:
        os.close(self.open_descriptor())

    def append(self, record: Mapping[str, object]) -> None:
        body = (
            json.dumps(dict(record), ensure_ascii=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
        descriptor = self.open_descriptor()
        try:
            offset = 0
            while offset < len(body):
                written = os.write(descriptor, body[offset:])
                if written <= 0:
                    raise OSError(f"{self.label} write failed")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
