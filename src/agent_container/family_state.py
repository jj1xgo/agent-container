from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any

from agent_container.github_broker_policy import validate_repository_id
from agent_container.state import Repository
from agent_container.state import ensure_private_directory
from agent_container.state import ensure_private_file


_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


@dataclass(frozen=True)
class FamilyBinding:
    repository: Repository
    repository_id: int


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _validate_private_directory(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("family binding parent is invalid")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise PermissionError("family binding parent is not private")
    if metadata.st_uid != os.getuid():
        raise PermissionError("family binding parent is not private")


def _validate_private_file(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("family binding file is invalid")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise PermissionError("family binding file is not private")
    if metadata.st_uid != os.getuid():
        raise PermissionError("family binding file is not private")


def _open_private_parent(path: Path) -> int:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise ValueError("family binding path is invalid")
    try:
        ensure_private_directory(path.parent)
    except PermissionError:
        raise
    except (OSError, ValueError):
        raise ValueError("family binding parent is invalid") from None

    components = path.parent.parts
    descriptor = os.open(os.sep, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC)
    try:
        private_ancestor_seen = False
        for component in components[1:]:
            before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            child = os.open(
                component,
                os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
                dir_fd=descriptor,
            )
            opened = os.fstat(child)
            os.close(descriptor)
            descriptor = child
            if not _same_inode(before, opened):
                raise ValueError("family binding parent changed")
            if private_ancestor_seen:
                _validate_private_directory(opened)
            elif (
                stat.S_ISDIR(opened.st_mode)
                and stat.S_IMODE(opened.st_mode) == 0o700
                and opened.st_uid == os.getuid()
            ):
                private_ancestor_seen = True
        _validate_private_directory(os.fstat(descriptor))
        return descriptor
    except PermissionError:
        os.close(descriptor)
        raise
    except OSError:
        os.close(descriptor)
        raise ValueError("family binding parent is invalid") from None
    except BaseException:
        os.close(descriptor)
        raise


def _entry_stat(parent_descriptor: int, name: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError:
        raise ValueError("family binding file is invalid") from None


def _open_private_file(parent_descriptor: int, name: str) -> tuple[int, os.stat_result]:
    before = _entry_stat(parent_descriptor, name)
    _validate_private_file(before)
    try:
        descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=parent_descriptor)
    except OSError:
        raise ValueError("family binding file is invalid") from None
    try:
        opened = os.fstat(descriptor)
        _validate_private_file(opened)
        if not _same_inode(before, opened):
            raise ValueError("family binding file changed")
        return descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def _read_private_bytes(path: Path, maximum_bytes: int) -> bytes:
    parent_descriptor = _open_private_parent(path)
    try:
        try:
            ensure_private_file(path)
        except PermissionError:
            raise
        except (OSError, ValueError):
            raise ValueError("family binding file is invalid") from None
        descriptor, expected = _open_private_file(parent_descriptor, path.name)
        try:
            body = bytearray()
            while len(body) <= maximum_bytes:
                chunk = os.read(descriptor, min(4096, maximum_bytes + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            if len(body) > maximum_bytes:
                raise ValueError("family binding is invalid")
        finally:
            os.close(descriptor)
        current = _entry_stat(parent_descriptor, path.name)
        _validate_private_file(current)
        if not _same_inode(expected, current):
            raise ValueError("family binding file changed")
        return bytes(body)
    finally:
        os.close(parent_descriptor)


def _without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("family binding is invalid")
        result[key] = value
    return result


def _read_private_json(path: Path, maximum_bytes: int) -> dict[str, Any]:
    try:
        payload = json.loads(
            _read_private_bytes(path, maximum_bytes).decode("utf-8"),
            object_pairs_hook=_without_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("family binding is invalid") from None
    if not isinstance(payload, dict):
        raise ValueError("family binding is invalid")
    return payload


def _exact_text(value: object) -> str:
    if type(value) is not str:
        raise ValueError("family binding is invalid")
    return value


def _decode_binding(body: bytes) -> FamilyBinding:
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_without_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        if not isinstance(payload, dict) or set(payload) != {
            "repository",
            "repository_id",
        }:
            raise ValueError()
        repository_text = _exact_text(payload["repository"])
        repository = Repository.parse(repository_text)
        if repository.slug != repository_text or repository_text != repository_text.lower():
            raise ValueError()
        return FamilyBinding(repository, validate_repository_id(payload["repository_id"]))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("family binding is invalid") from None


def _validate_binding(binding: FamilyBinding) -> FamilyBinding:
    if type(binding) is not FamilyBinding:
        raise ValueError("family binding is invalid")
    repository = binding.repository
    if type(repository) is not Repository or repository.slug != repository.slug.lower():
        raise ValueError("family binding is invalid")
    try:
        Repository.parse(repository.slug)
        repository_id = validate_repository_id(binding.repository_id)
    except ValueError:
        raise ValueError("family binding is invalid") from None
    return FamilyBinding(repository, repository_id)


def load_family_binding(path: Path) -> FamilyBinding:
    return _decode_binding(_read_private_bytes(path, maximum_bytes=4096))


def _encode_binding(binding: FamilyBinding) -> bytes:
    binding = _validate_binding(binding)
    return (
        json.dumps(
            {
                "repository": binding.repository.slug,
                "repository_id": binding.repository_id,
            },
            ensure_ascii=True,
            indent=2,
        )
        + "\n"
    ).encode("ascii")


def _write_all(descriptor: int, body: bytes) -> None:
    remaining = memoryview(body)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("family binding write failed")
        remaining = remaining[written:]


def _snapshot(parent_descriptor: int, name: str) -> tuple[bytes, os.stat_result]:
    descriptor, expected = _open_private_file(parent_descriptor, name)
    try:
        body = bytearray()
        while len(body) <= 4096:
            chunk = os.read(descriptor, min(4096, 4097 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        if len(body) > 4096:
            raise ValueError("family binding is invalid")
    finally:
        os.close(descriptor)
    current = _entry_stat(parent_descriptor, name)
    _validate_private_file(current)
    if not _same_inode(expected, current):
        raise ValueError("family binding file changed")
    _decode_binding(bytes(body))
    return bytes(body), expected


def _temporary_name(name: str) -> str:
    return f".{name}.{secrets.token_hex(16)}"


def _unlink_owned(parent_descriptor: int, name: str, expected: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _same_inode(current, expected):
        os.unlink(name, dir_fd=parent_descriptor)


def write_family_binding(path: Path, binding: FamilyBinding) -> None:
    body = _encode_binding(binding)
    parent_descriptor = _open_private_parent(path)
    temporary: str | None = None
    temporary_stat: os.stat_result | None = None
    published = False
    descriptor: int | None = None
    try:
        try:
            _existing_body, original = _snapshot(parent_descriptor, path.name)
        except ValueError as error:
            try:
                os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                original = None
            else:
                raise error
        temporary = _temporary_name(path.name)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
            0o600,
            dir_fd=parent_descriptor,
        )
        temporary_stat = os.fstat(descriptor)
        os.fchmod(descriptor, 0o600)
        _validate_private_file(os.fstat(descriptor))
        _write_all(descriptor, body)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if original is None:
            os.link(
                temporary,
                path.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            published = True
            _unlink_owned(parent_descriptor, temporary, temporary_stat)
        else:
            current = _entry_stat(parent_descriptor, path.name)
            _validate_private_file(current)
            if not _same_inode(original, current):
                raise ValueError("family binding file changed")
            os.fsync(parent_descriptor)
            os.replace(
                temporary,
                path.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            published = True
        os.fsync(parent_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None and temporary_stat is not None and not published:
            _unlink_owned(parent_descriptor, temporary, temporary_stat)
        os.close(parent_descriptor)
