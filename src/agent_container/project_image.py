from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import os
import re
import stat


_PACKAGE_RE = re.compile(
    r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9]+)?(?:=[A-Za-z0-9.+:~_-]+)?"
)
_NODE_VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
_ALLOWED_FILES = frozenset({"packages.txt", "node-version.txt"})
_MAX_CONFIG_BYTES = 64 * 1024
_HASH_INPUT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/+-]*")
_ARCHITECTURE_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
_PROJECT_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
_KEY_RE = re.compile(r"[0-9a-f]{64}")
DERIVED_IMAGE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ProjectImageConfig:
    packages: tuple[str, ...]
    node_version: str | None

    @property
    def is_empty(self) -> bool:
        return not self.packages and self.node_version is None


def project_image_key(
    base_image_id: str, config: ProjectImageConfig, architecture: str
) -> str:
    if _HASH_INPUT_RE.fullmatch(base_image_id) is None:
        raise ValueError("invalid base image identity")
    if _ARCHITECTURE_RE.fullmatch(architecture) is None:
        raise ValueError("invalid image architecture")
    payload = {
        "architecture": architecture,
        "base_image_id": base_image_id,
        "node_version": config.node_version,
        "packages": list(config.packages),
        "schema": DERIVED_IMAGE_SCHEMA_VERSION,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def project_image_name(project_id: str, key: str) -> str:
    if _PROJECT_ID_RE.fullmatch(project_id) is None or len(project_id) > 63:
        raise ValueError("invalid project ID for image name")
    if _KEY_RE.fullmatch(key) is None:
        raise ValueError("invalid project image key")
    return f"localhost/agent-container-project:{project_id}-{key[:16]}"


def _write_generated_file(directory_fd: int, name: str, contents: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    except OSError as error:
        raise ValueError("project build context is not safely writable") from error
    try:
        payload = contents.encode("utf-8")
        written = 0
        while written < len(payload):
            written += os.write(file_fd, payload[written:])
        os.fchmod(file_fd, 0o600)
    finally:
        os.close(file_fd)


def write_project_build_context(
    root: Path, base_image: str, config: ProjectImageConfig
) -> Path:
    root = Path(root)
    metadata = _lstat_or_none(root)
    if (
        metadata is None
        or not stat.S_ISDIR(metadata.st_mode)
        or root.resolve(strict=True) != Path(os.path.abspath(root))
    ):
        raise ValueError("project build context must be a real directory")
    if _HASH_INPUT_RE.fullmatch(base_image) is None:
        raise ValueError("invalid base image")
    if any(_PACKAGE_RE.fullmatch(package) is None for package in config.packages):
        raise ValueError("invalid project package configuration")
    if config.node_version is not None and _NODE_VERSION_RE.fullmatch(
        config.node_version
    ) is None:
        raise ValueError("invalid project Node version configuration")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(root, directory_flags)
    except OSError as error:
        raise ValueError("project build context is not safely accessible") from error
    try:
        if os.listdir(directory_fd):
            raise ValueError("project build context must be empty")
        os.fchmod(directory_fd, 0o700)

        lines = ["ARG BASE_IMAGE", "FROM ${BASE_IMAGE}", "", "USER root"]
        if config.packages:
            lines.extend(
                (
                    "COPY packages.txt /tmp/project-packages.txt",
                    "RUN apt-get update \\",
                    "    && xargs --no-run-if-empty apt-get install -y "
                    "--no-install-recommends < /tmp/project-packages.txt \\",
                    "    && rm -f /tmp/project-packages.txt \\",
                    "    && rm -rf /var/lib/apt/lists/*",
                )
            )
            normalized = "".join(
                f"{package}\n" for package in sorted(set(config.packages))
            )
            _write_generated_file(directory_fd, "packages.txt", normalized)

        if config.node_version is not None:
            version = config.node_version
            lines.extend(
                (
                    "RUN set -eux; \\",
                    "    case \"$(dpkg --print-architecture)\" in \\",
                    "      amd64) node_arch=x64 ;; \\",
                    "      arm64) node_arch=arm64 ;; \\",
                    "      *) echo \"unsupported project Node architecture\" >&2; "
                    "exit 1 ;; \\",
                    "    esac; \\",
                    f'    archive="node-v{version}-linux-${{node_arch}}.tar.xz"; \\',
                    f'    curl -fsSLO "https://nodejs.org/dist/v{version}/'
                    '${archive}"; \\',
                    f'    curl -fsSLO "https://nodejs.org/dist/v{version}/'
                    'SHASUMS256.txt"; \\',
                    '    grep "  ${archive}$" SHASUMS256.txt | sha256sum '
                    "--check --strict -; \\",
                    "    mkdir -p /opt/project-node; \\",
                    '    tar -xJf "${archive}" -C /opt/project-node '
                    "--strip-components=1; \\",
                    '    rm -f "${archive}" SHASUMS256.txt',
                    "RUN printf '%s\\n' 'PATH=/opt/project-node/bin:$PATH' "
                    "'export PATH' > /etc/profile.d/20-project-node.sh",
                    "ENV PATH=/opt/project-node/bin:/usr/local/bin:"
                    "/opt/agent-node/bin:/usr/local/sbin:/usr/sbin:"
                    "/usr/bin:/sbin:/bin",
                )
            )

        lines.extend(("", "USER agent", ""))
        _write_generated_file(directory_fd, "Containerfile", "\n".join(lines))
    finally:
        os.close(directory_fd)
    return root / "Containerfile"


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
