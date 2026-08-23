from dataclasses import dataclass
import ctypes
import errno
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
from typing import Any


DEFAULT_FILES = ("CLAUDE.md", "settings.json")
DEFAULT_DIRECTORIES = ("agents", "commands", "rules", "skills", "hooks")
DENIED_TOP_LEVEL = frozenset(
    {
        ".credentials.json",
        ".claude.json",
        ".git",
        "projects",
        "sessions",
        "transcripts",
        "handovers",
        "plans",
        "state",
        "cache",
        "logs",
        "test-results",
        "scratchpad",
    }
)
SECRET_ENV_KEY = re.compile(
    r"TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY|AUTH", re.IGNORECASE
)


@dataclass(frozen=True)
class MigrationEntry:
    relative_path: Path
    is_directory: bool
    executable: bool


@dataclass(frozen=True)
class GeneratedFile:
    relative_path: Path
    body: bytes
    executable: bool = False


@dataclass(frozen=True)
class MigrationPlan:
    source: Path
    destination: Path
    entries: tuple[MigrationEntry, ...]
    generated_files: tuple[GeneratedFile, ...]
    skipped: tuple[Path, ...]


class _DestinationExistsError(FileExistsError):
    pass


def _lstat_if_present(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _validate_source_root(source: Path) -> Path:
    if not source.is_absolute():
        raise ValueError("source must be absolute")
    source_stat = _lstat_if_present(source)
    if source_stat is None:
        raise FileNotFoundError(source)
    if stat.S_ISLNK(source_stat.st_mode):
        raise ValueError(f"source must not be a symlink: {source}")
    if not stat.S_ISDIR(source_stat.st_mode):
        raise NotADirectoryError(source)
    resolved = source.resolve(strict=True)
    if resolved != source:
        raise ValueError(f"source must be a canonical path: {source}")
    return resolved


def _validate_destination_text(destination: Path) -> None:
    if not destination.is_absolute():
        raise ValueError("destination must be absolute")
    if any(
        ord(character) < 32 or ord(character) == 127
        for character in destination.as_posix()
    ):
        raise ValueError("destination name must not contain control characters")


def _validate_destination(destination: Path) -> None:
    _validate_destination_text(destination)
    if _lstat_if_present(destination) is not None:
        raise _DestinationExistsError("migration destination already exists")


def _safe_relative_path(relative_path: Path) -> Path:
    if any(
        ord(character) < 32 or ord(character) == 127
        for part in relative_path.parts
        for character in part
    ):
        raise ValueError("migration path must not contain control characters")
    if relative_path.is_absolute() or not relative_path.parts:
        raise ValueError(f"migration path must be relative: {relative_path}")
    if any(part in {"", ".", ".."} for part in relative_path.parts):
        raise ValueError(f"migration path must not escape destination: {relative_path}")
    return relative_path


def _validate_source_path(source: Path, path: Path) -> os.stat_result:
    path_stat = _lstat_if_present(path)
    if path_stat is None:
        raise FileNotFoundError(path)
    if stat.S_ISLNK(path_stat.st_mode):
        raise ValueError(f"allowlisted path must not be a symlink: {path}")
    try:
        path.resolve(strict=True).relative_to(source)
    except ValueError:
        raise ValueError(f"allowlisted path escapes source: {path}") from None
    return path_stat


def _entry_for_path(source: Path, path: Path) -> MigrationEntry:
    path_stat = _validate_source_path(source, path)
    if stat.S_ISDIR(path_stat.st_mode):
        is_directory = True
        executable = False
    elif stat.S_ISREG(path_stat.st_mode):
        is_directory = False
        executable = bool(path_stat.st_mode & stat.S_IXUSR)
    else:
        raise ValueError(f"allowlisted path must be a regular file or directory: {path}")
    try:
        relative_path = path.relative_to(source)
    except ValueError:
        raise ValueError(f"allowlisted path escapes source: {path}") from None
    _safe_relative_path(relative_path)
    return MigrationEntry(relative_path, is_directory, executable)


def _walk_allowlisted_directory(
    source: Path,
    directory: Path,
    entries: list[MigrationEntry],
    skipped: list[Path],
) -> None:
    entry = _entry_for_path(source, directory)
    if not entry.is_directory:
        raise ValueError(f"allowlisted directory is not a directory: {directory}")
    entries.append(entry)
    for child in directory.iterdir():
        relative_path = child.relative_to(source)
        if child.name in DENIED_TOP_LEVEL:
            skipped.append(_safe_relative_path(relative_path))
            continue
        child_entry = _entry_for_path(source, child)
        if child_entry.is_directory:
            _walk_allowlisted_directory(source, child, entries, skipped)
        else:
            entries.append(child_entry)


def _validate_settings_node(node: Any) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "apiKeyHelper":
                raise ValueError("settings.json must not contain apiKeyHelper")
            if key == "env" and isinstance(value, dict):
                for env_name in value:
                    if SECRET_ENV_KEY.search(str(env_name)):
                        raise ValueError(
                            f"settings.json env name is sensitive: {env_name}"
                        )
            _validate_settings_node(value)
    elif isinstance(node, list):
        for value in node:
            _validate_settings_node(value)


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("settings.json must not contain duplicate object keys")
        result[key] = value
    return result


def _validate_settings_bytes(body: bytes) -> None:
    try:
        payload = json.loads(body, object_pairs_hook=_reject_duplicate_object_keys)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("settings.json must contain valid JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("settings.json must contain a JSON object")
    _validate_settings_node(payload)


def _open_absolute_directory_nofollow(path: Path, label: str) -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise RuntimeError("descriptor-safe migration is unavailable on this platform")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(Path(path.anchor), flags)
    try:
        for part in path.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError as error:
        os.close(descriptor)
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError(f"{label} must not contain symlinks") from None
        raise
    return descriptor


def _open_relative_source(
    source_descriptor: int, relative_path: Path, *, is_directory: bool
) -> int:
    relative_path = _safe_relative_path(relative_path)
    descriptor = os.dup(source_descriptor)
    try:
        for index, part in enumerate(relative_path.parts):
            final = index == len(relative_path.parts) - 1
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
            if not final or is_directory:
                flags |= os.O_DIRECTORY
            elif final:
                flags |= os.O_NONBLOCK
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError as error:
        os.close(descriptor)
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError(
                "planned source entry changed type or became a symlink"
            ) from None
        raise
    return descriptor


def _read_descriptor(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _validate_settings(source: Path, relative_path: Path) -> None:
    source_descriptor = _open_absolute_directory_nofollow(source, "source")
    settings_descriptor = -1
    try:
        settings_descriptor = _open_relative_source(
            source_descriptor, relative_path, is_directory=False
        )
        if not stat.S_ISREG(os.fstat(settings_descriptor).st_mode):
            raise ValueError("settings.json must be a regular file")
        _validate_settings_bytes(_read_descriptor(settings_descriptor))
    finally:
        if settings_descriptor >= 0:
            os.close(settings_descriptor)
        os.close(source_descriptor)


def plan_claude_migration(source: Path, destination: Path) -> MigrationPlan:
    source = _validate_source_root(source)
    _validate_destination(destination)

    entries: list[MigrationEntry] = []
    skipped: list[Path] = []
    for name in DEFAULT_FILES:
        path = source / name
        if _lstat_if_present(path) is None:
            continue
        entry = _entry_for_path(source, path)
        if entry.is_directory:
            raise ValueError(f"allowlisted file is not a regular file: {path}")
        entries.append(entry)
        if name == "settings.json":
            _validate_settings(source, Path(name))

    for name in DEFAULT_DIRECTORIES:
        path = source / name
        if _lstat_if_present(path) is None:
            continue
        _walk_allowlisted_directory(source, path, entries, skipped)

    skipped.extend(
        Path(name)
        for name in DENIED_TOP_LEVEL
        if _lstat_if_present(source / name) is not None
    )
    entries.sort(key=lambda entry: entry.relative_path.as_posix())
    skipped.sort(key=Path.as_posix)
    return MigrationPlan(source, destination, tuple(entries), (), tuple(skipped))


def _validate_planned_source_entry(
    source_descriptor: int, entry: MigrationEntry
) -> tuple[int, os.stat_result]:
    relative_path = _safe_relative_path(entry.relative_path)
    descriptor = _open_relative_source(
        source_descriptor, relative_path, is_directory=entry.is_directory
    )
    path_stat = os.fstat(descriptor)
    if entry.is_directory:
        if not stat.S_ISDIR(path_stat.st_mode):
            os.close(descriptor)
            raise ValueError(f"planned directory changed type: {relative_path}")
    else:
        if not stat.S_ISREG(path_stat.st_mode):
            os.close(descriptor)
            raise ValueError(f"planned file changed type: {relative_path}")
        executable = bool(path_stat.st_mode & stat.S_IXUSR)
        if executable != entry.executable:
            os.close(descriptor)
            raise ValueError(f"planned file changed executable mode: {relative_path}")
    return descriptor, path_stat


def _open_stage_directory(stage_descriptor: int, relative_path: Path) -> int:
    descriptor = os.dup(stage_descriptor)
    try:
        for part in relative_path.parts:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            next_descriptor = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.fchmod(next_descriptor, 0o700)
            os.close(descriptor)
            descriptor = next_descriptor
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_staged_file(
    stage_descriptor: int, relative_path: Path, mode: int
) -> int:
    relative_path = _safe_relative_path(relative_path)
    parent = Path(*relative_path.parts[:-1])
    parent_descriptor = _open_stage_directory(stage_descriptor, parent)
    try:
        descriptor = os.open(
            relative_path.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC,
            mode,
            dir_fd=parent_descriptor,
        )
    finally:
        os.close(parent_descriptor)
    return descriptor


def _write_all(descriptor: int, body: bytes) -> None:
    view = memoryview(body)
    while view:
        written = os.write(descriptor, view)
        view = view[written:]


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _clear_directory_descriptor(descriptor: int) -> None:
    for name in os.listdir(descriptor):
        before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            child_descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            try:
                if not _same_identity(before, os.fstat(child_descriptor)):
                    continue
                _clear_directory_descriptor(child_descriptor)
            finally:
                os.close(child_descriptor)
            after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if _same_identity(before, after):
                os.rmdir(name, dir_fd=descriptor)
        else:
            after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if _same_identity(before, after):
                os.unlink(name, dir_fd=descriptor)


def _remove_known_stage(
    parent_descriptor: int,
    stage_name: str,
    stage_descriptor: int,
    stage_identity: os.stat_result,
) -> None:
    try:
        named_identity = os.stat(
            stage_name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        if not _same_identity(named_identity, stage_identity):
            return
        if not _same_identity(os.fstat(stage_descriptor), stage_identity):
            return
        _clear_directory_descriptor(stage_descriptor)
        named_identity = os.stat(
            stage_name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        if _same_identity(named_identity, stage_identity):
            os.rmdir(stage_name, dir_fd=parent_descriptor)
    except OSError:
        return


def _create_outer_stage(
    parent_descriptor: int, destination_name: str
) -> tuple[str, int, os.stat_result]:
    prefix = f".{destination_name}.migrate-"
    for _ in range(128):
        stage_name = f"{prefix}{secrets.token_hex(16)}"
        try:
            os.mkdir(stage_name, mode=0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            continue

        created_identity = os.stat(
            stage_name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        stage_descriptor = -1
        try:
            stage_descriptor = os.open(
                stage_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_descriptor,
            )
            opened_identity = os.fstat(stage_descriptor)
            if not _same_identity(created_identity, opened_identity):
                raise RuntimeError("migration staging directory identity changed")
            os.fchmod(stage_descriptor, 0o700)
            return stage_name, stage_descriptor, opened_identity
        except BaseException:
            if stage_descriptor >= 0:
                os.close(stage_descriptor)
            try:
                current_identity = os.stat(
                    stage_name, dir_fd=parent_descriptor, follow_symlinks=False
                )
                if _same_identity(created_identity, current_identity):
                    os.rmdir(stage_name, dir_fd=parent_descriptor)
            except OSError:
                pass
            raise
    raise RuntimeError("unable to create private migration staging directory")


def _rename_noreplace(
    source_parent_descriptor: int,
    source_name: str,
    destination_parent_descriptor: int,
    destination_name: str,
) -> None:
    # Publishing is intentionally Linux-specific: libc renameat2 with
    # RENAME_NOREPLACE is the only atomic directory no-overwrite primitive
    # available to this host-side implementation. Missing support fails closed.
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("atomic no-replace migration publish is unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_parent_descriptor,
        os.fsencode(source_name),
        destination_parent_descriptor,
        os.fsencode(destination_name),
        1,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise _DestinationExistsError("migration destination already exists")
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        raise RuntimeError("atomic no-replace migration publish is unavailable")
    raise OSError(error_number, "atomic migration publish failed")


def apply_claude_migration(plan: MigrationPlan) -> Path:
    source = plan.source
    source_descriptor = -1
    parent_descriptor = -1
    stage_descriptor = -1
    payload_descriptor = -1
    stage_name: str | None = None
    stage_identity: os.stat_result | None = None
    published = False
    try:
        source = _validate_source_root(source)
        _validate_destination(plan.destination)
        source_descriptor = _open_absolute_directory_nofollow(source, "source")
        parent_descriptor = _open_absolute_directory_nofollow(
            plan.destination.parent, "destination parent"
        )
        stage_name, stage_descriptor, stage_identity = _create_outer_stage(
            parent_descriptor, plan.destination.name
        )
        os.mkdir("payload", mode=0o700, dir_fd=stage_descriptor)
        payload_descriptor = os.open(
            "payload",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=stage_descriptor,
        )
        os.fchmod(payload_descriptor, 0o700)

        for entry in plan.entries:
            entry_descriptor, _ = _validate_planned_source_entry(
                source_descriptor, entry
            )
            relative_path = _safe_relative_path(entry.relative_path)
            try:
                if entry.is_directory:
                    directory_descriptor = _open_stage_directory(
                        payload_descriptor, relative_path
                    )
                    os.close(directory_descriptor)
                    continue

                mode = 0o700 if entry.executable else 0o600
                target_descriptor = _open_staged_file(
                    payload_descriptor, relative_path, mode
                )
                try:
                    if relative_path == Path("settings.json"):
                        body = _read_descriptor(entry_descriptor)
                        _validate_settings_bytes(body)
                        _write_all(target_descriptor, body)
                    else:
                        with os.fdopen(
                            os.dup(entry_descriptor), "rb"
                        ) as source_stream, os.fdopen(
                            os.dup(target_descriptor), "wb"
                        ) as target_stream:
                            shutil.copyfileobj(source_stream, target_stream)
                    os.fchmod(target_descriptor, mode)
                finally:
                    os.close(target_descriptor)
            finally:
                os.close(entry_descriptor)

        for generated in plan.generated_files:
            relative_path = _safe_relative_path(generated.relative_path)
            mode = 0o700 if generated.executable else 0o600
            target_descriptor = _open_staged_file(
                payload_descriptor, relative_path, mode
            )
            try:
                _write_all(target_descriptor, generated.body)
                os.fchmod(target_descriptor, mode)
            finally:
                os.close(target_descriptor)

        _validate_destination(plan.destination)
        _rename_noreplace(
            stage_descriptor,
            "payload",
            parent_descriptor,
            plan.destination.name,
        )
        published = True
        _remove_known_stage(
            parent_descriptor, stage_name, stage_descriptor, stage_identity
        )
        return plan.destination
    except _DestinationExistsError:
        if (
            not published
            and parent_descriptor >= 0
            and stage_descriptor >= 0
            and stage_name is not None
            and stage_identity is not None
        ):
            _remove_known_stage(
                parent_descriptor, stage_name, stage_descriptor, stage_identity
            )
        raise FileExistsError("migration destination already exists") from None
    except OSError:
        if (
            not published
            and parent_descriptor >= 0
            and stage_descriptor >= 0
            and stage_name is not None
            and stage_identity is not None
        ):
            _remove_known_stage(
                parent_descriptor, stage_name, stage_descriptor, stage_identity
            )
        raise RuntimeError("migration filesystem operation failed") from None
    except BaseException:
        if (
            not published
            and parent_descriptor >= 0
            and stage_descriptor >= 0
            and stage_name is not None
            and stage_identity is not None
        ):
            _remove_known_stage(
                parent_descriptor, stage_name, stage_descriptor, stage_identity
            )
        raise
    finally:
        if payload_descriptor >= 0:
            os.close(payload_descriptor)
        if stage_descriptor >= 0:
            os.close(stage_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)


def render_migration_plan(plan: MigrationPlan) -> tuple[str, ...]:
    _validate_destination_text(plan.destination)
    lines = []
    for entry in plan.entries:
        relative_path = _safe_relative_path(entry.relative_path)
        if entry.is_directory:
            continue
        kind = "executable" if entry.executable else "file"
        lines.append(f"COPY {kind} {relative_path.as_posix()}")
    for generated in plan.generated_files:
        relative_path = _safe_relative_path(generated.relative_path)
        kind = "executable" if generated.executable else "file"
        lines.append(f"COPY {kind} {relative_path.as_posix()}")
    lines.extend(
        f"SKIP denied {_safe_relative_path(path).as_posix()}" for path in plan.skipped
    )
    lines.append(f"DESTINATION {plan.destination.as_posix()}")
    return tuple(lines)
