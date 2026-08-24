from dataclasses import dataclass
from pathlib import Path
import os
import re
import stat


_PACKAGE_RE = re.compile(
    r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9]+)?(?:=[A-Za-z0-9.+:~_-]+)?"
)
_NODE_VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
_ALLOWED_FILES = frozenset({"packages.txt", "node-version.txt"})
_MAX_CONFIG_BYTES = 64 * 1024


@dataclass(frozen=True)
class ProjectImageConfig:
    packages: tuple[str, ...]
    node_version: str | None

    @property
    def is_empty(self) -> bool:
        return not self.packages and self.node_version is None


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError("project image configuration is not accessible") from error


def _read_config_file(config_fd: int, name: str) -> str | None:
    try:
        metadata = os.stat(name, dir_fd=config_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError(f"project image {name} is not accessible") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_CONFIG_BYTES:
        raise ValueError(f"project image {name} must be a small regular file")

    flags = os.O_RDONLY | os.O_NONBLOCK
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(name, flags, dir_fd=config_fd)
    except OSError as error:
        raise ValueError(f"project image {name} is not safely readable") from error
    try:
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > _MAX_CONFIG_BYTES:
            raise ValueError(f"project image {name} must be a small regular file")
        chunks: list[bytes] = []
        remaining = _MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(file_fd, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > _MAX_CONFIG_BYTES:
            raise ValueError(f"project image {name} is too large")
        try:
            return payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValueError(f"project image {name} must be UTF-8 text") from error
    finally:
        os.close(file_fd)


def load_project_image_config(workspace: Path) -> ProjectImageConfig:
    workspace = Path(workspace)
    workspace_metadata = _lstat_or_none(workspace)
    if (
        workspace_metadata is None
        or not stat.S_ISDIR(workspace_metadata.st_mode)
        or workspace.resolve(strict=True) != Path(os.path.abspath(workspace))
    ):
        raise ValueError("workspace must be a real directory without symlinks")

    if _lstat_or_none(workspace / ".claude-container.d") is not None:
        raise ValueError(".claude-container.d is unsupported; use .agent-container.d")

    config_root = workspace / ".agent-container.d"
    config_metadata = _lstat_or_none(config_root)
    if config_metadata is None:
        return ProjectImageConfig((), None)
    if not stat.S_ISDIR(config_metadata.st_mode):
        raise ValueError(".agent-container.d must be a real directory")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        config_fd = os.open(config_root, directory_flags)
    except OSError as error:
        raise ValueError(".agent-container.d is not safely accessible") from error
    try:
        entries = set(os.listdir(config_fd))
        unknown = entries - _ALLOWED_FILES
        if unknown:
            raise ValueError(".agent-container.d contains unsupported entries")

        packages_text = _read_config_file(config_fd, "packages.txt")
        node_text = _read_config_file(config_fd, "node-version.txt")
    finally:
        os.close(config_fd)

    packages: set[str] = set()
    if packages_text is not None:
        for raw_line in packages_text.splitlines():
            package = raw_line.strip()
            if not package or package.startswith("#"):
                continue
            if _PACKAGE_RE.fullmatch(package) is None:
                raise ValueError("invalid project package configuration")
            packages.add(package)

    node_version: str | None = None
    if node_text is not None:
        lines = node_text.splitlines()
        if len(lines) != 1 or _NODE_VERSION_RE.fullmatch(lines[0]) is None:
            raise ValueError("invalid project Node version configuration")
        node_version = lines[0]

    return ProjectImageConfig(tuple(sorted(packages)), node_version)
