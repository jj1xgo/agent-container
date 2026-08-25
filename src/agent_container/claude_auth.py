import os
from pathlib import Path
import secrets
import stat

from agent_container.state import StateLayout
from agent_container.state import ensure_private_directory
from agent_container.state import ensure_private_file
from agent_container.state import validate_claude_oauth_token


_STAGING_PREFIX = ".oauth-token-stage-"


def _staging_path(auth_dir: Path) -> Path:
    return auth_dir / f"{_STAGING_PREFIX}{secrets.token_hex()}"


def _is_staging_path(path: Path) -> bool:
    suffix = path.name.removeprefix(_STAGING_PREFIX)
    return (
        path.name.startswith(_STAGING_PREFIX)
        and len(suffix) == 64
        and all(character in "0123456789abcdef" for character in suffix)
    )


def stage_claude_token(auth_dir: Path, token: str) -> Path:
    validate_claude_oauth_token(token)
    auth_dir = ensure_private_directory(auth_dir)
    staged = _staging_path(auth_dir)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(staged, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        ensure_private_file(staged)
        validate_claude_oauth_token(staged.read_text(encoding="ascii"))
    except BaseException:
        try:
            staged.unlink()
        except OSError:
            pass
        finally:
            raise
    return staged


def install_claude_token(staged: Path, destination: Path) -> None:
    destination_parent = ensure_private_directory(destination.parent)
    staged_parent = ensure_private_directory(staged.parent)
    if staged_parent != destination_parent or not _is_staging_path(staged):
        raise ValueError("staged Claude OAuth token is not in the destination directory")
    ensure_private_file(staged)
    validate_claude_oauth_token(staged.read_text(encoding="ascii"))
    if destination.is_symlink() or destination.exists():
        ensure_private_file(destination)
    os.replace(staged, destination)


def discard_staged_token(staged: Path) -> None:
    ensure_private_directory(staged.parent)
    if not _is_staging_path(staged):
        raise ValueError("refusing to discard a non-staging file")
    try:
        details = os.lstat(staged)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
        raise ValueError("refusing to discard an unsafe staging file")
    staged.unlink()


def _validate_legacy_entry(path: Path, expect_directory: bool | None) -> None:
    details = os.lstat(path)
    if stat.S_ISLNK(details.st_mode):
        raise ValueError("legacy Claude state must not contain symlinks")
    if stat.S_ISDIR(details.st_mode):
        if expect_directory is False:
            raise ValueError("legacy Claude state entries must be regular files")
        with os.scandir(path) as entries:
            for entry in entries:
                _validate_legacy_entry(Path(entry.path), expect_directory=None)
        return
    if stat.S_ISREG(details.st_mode):
        if expect_directory is True:
            raise ValueError("legacy Claude backups must be a directory")
        return
    raise ValueError("legacy Claude state entries must be regular files")


def validate_legacy_quarantine_sources(layout: StateLayout) -> tuple[Path, ...]:
    ensure_private_directory(layout.claude_auth_dir)
    candidates = (
        (layout.claude_legacy_credentials_file, False),
        (layout.claude_legacy_metadata_file, False),
        (layout.claude_legacy_backups, True),
    )
    sources: list[Path] = []
    for path, expect_directory in candidates:
        try:
            os.lstat(path)
        except FileNotFoundError:
            continue
        _validate_legacy_entry(path, expect_directory)
        sources.append(path)
    return tuple(sources)


def _create_private_directory(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    return ensure_private_directory(path)


def _normalize_quarantine_tree(path: Path) -> None:
    details = os.lstat(path)
    if stat.S_ISLNK(details.st_mode):
        raise ValueError("legacy Claude state must not contain symlinks")
    if stat.S_ISDIR(details.st_mode):
        os.chmod(path, 0o700, follow_symlinks=False)
        with os.scandir(path) as entries:
            for entry in entries:
                _normalize_quarantine_tree(Path(entry.path))
        return
    if stat.S_ISREG(details.st_mode):
        os.chmod(path, 0o600, follow_symlinks=False)
        return
    raise ValueError("legacy Claude state entries must be regular files")


def _validate_quarantine_nonce(nonce: str) -> str:
    if (
        not nonce
        or nonce in {".", ".."}
        or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in nonce)
    ):
        raise ValueError("Claude quarantine nonce has invalid format")
    return nonce


def quarantine_legacy_claude_state(
    layout: StateLayout, sources: tuple[Path, ...], nonce: str | None = None
) -> Path | None:
    if not sources:
        if validate_legacy_quarantine_sources(layout):
            raise ValueError("legacy Claude state changed before quarantine")
        return None
    if validate_legacy_quarantine_sources(layout) != sources:
        raise ValueError("legacy Claude state changed before quarantine")
    ensure_private_directory(layout.root)
    quarantine_parent = _create_private_directory(layout.root / "quarantine")
    quarantine_root = _create_private_directory(quarantine_parent / "claude")
    name = _validate_quarantine_nonce(secrets.token_hex() if nonce is None else nonce)
    quarantine = quarantine_root / name
    quarantine.mkdir(mode=0o700)
    ensure_private_directory(quarantine)
    if validate_legacy_quarantine_sources(layout) != sources:
        raise ValueError("legacy Claude state changed before quarantine")
    for source in sources:
        os.replace(source, quarantine / source.name)
        _normalize_quarantine_tree(quarantine / source.name)
    return quarantine
