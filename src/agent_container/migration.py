from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
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


def _validate_destination(destination: Path) -> None:
    if not destination.is_absolute():
        raise ValueError("destination must be absolute")
    if _lstat_if_present(destination) is not None:
        raise FileExistsError(destination)


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
    return MigrationEntry(relative_path, is_directory, executable)


def _walk_allowlisted_directory(
    source: Path, directory: Path, entries: list[MigrationEntry]
) -> None:
    entry = _entry_for_path(source, directory)
    if not entry.is_directory:
        raise ValueError(f"allowlisted directory is not a directory: {directory}")
    entries.append(entry)
    for child in directory.iterdir():
        child_entry = _entry_for_path(source, child)
        if child_entry.is_directory:
            _walk_allowlisted_directory(source, child, entries)
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


def _validate_settings(source: Path, settings_path: Path) -> None:
    path_stat = _validate_source_path(source, settings_path)
    if not stat.S_ISREG(path_stat.st_mode):
        raise ValueError("settings.json must be a regular file")
    try:
        payload = json.loads(settings_path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("settings.json must contain valid JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("settings.json must contain a JSON object")
    _validate_settings_node(payload)


def plan_claude_migration(source: Path, destination: Path) -> MigrationPlan:
    source = _validate_source_root(source)
    _validate_destination(destination)

    entries: list[MigrationEntry] = []
    for name in DEFAULT_FILES:
        path = source / name
        if _lstat_if_present(path) is None:
            continue
        entry = _entry_for_path(source, path)
        if entry.is_directory:
            raise ValueError(f"allowlisted file is not a regular file: {path}")
        entries.append(entry)
        if name == "settings.json":
            _validate_settings(source, path)

    for name in DEFAULT_DIRECTORIES:
        path = source / name
        if _lstat_if_present(path) is None:
            continue
        _walk_allowlisted_directory(source, path, entries)

    skipped = tuple(
        Path(name)
        for name in sorted(DENIED_TOP_LEVEL)
        if _lstat_if_present(source / name) is not None
    )
    entries.sort(key=lambda entry: entry.relative_path.as_posix())
    return MigrationPlan(source, destination, tuple(entries), (), skipped)


def _safe_relative_path(relative_path: Path) -> Path:
    if relative_path.is_absolute() or not relative_path.parts:
        raise ValueError(f"migration path must be relative: {relative_path}")
    if any(part in {"", ".", ".."} for part in relative_path.parts):
        raise ValueError(f"migration path must not escape destination: {relative_path}")
    return relative_path


def _validate_planned_source_entry(
    source: Path, entry: MigrationEntry
) -> tuple[Path, os.stat_result]:
    relative_path = _safe_relative_path(entry.relative_path)
    path = source / relative_path
    path_stat = _validate_source_path(source, path)
    if entry.is_directory:
        if not stat.S_ISDIR(path_stat.st_mode):
            raise ValueError(f"planned directory changed type: {relative_path}")
    else:
        if not stat.S_ISREG(path_stat.st_mode):
            raise ValueError(f"planned file changed type: {relative_path}")
        executable = bool(path_stat.st_mode & stat.S_IXUSR)
        if executable != entry.executable:
            raise ValueError(f"planned file changed executable mode: {relative_path}")
    return path, path_stat


def _ensure_private_parents(stage: Path, parent: Path) -> None:
    relative_parent = parent.relative_to(stage)
    current = stage
    for part in relative_parent.parts:
        current = current / part
        current_stat = _lstat_if_present(current)
        if current_stat is None:
            current.mkdir(mode=0o700)
        elif not stat.S_ISDIR(current_stat.st_mode) or stat.S_ISLNK(
            current_stat.st_mode
        ):
            raise ValueError(f"migration parent is not a directory: {current}")
        current.chmod(0o700)


def _remove_known_stage(stage: Path) -> None:
    stage_stat = _lstat_if_present(stage)
    if stage_stat is None:
        return
    if stat.S_ISDIR(stage_stat.st_mode) and not stat.S_ISLNK(stage_stat.st_mode):
        if stage.resolve(strict=True) == stage:
            shutil.rmtree(stage)


def apply_claude_migration(plan: MigrationPlan) -> Path:
    source = _validate_source_root(plan.source)
    _validate_destination(plan.destination)

    stage: Path | None = None
    try:
        stage = Path(
            tempfile.mkdtemp(
                prefix=f".{plan.destination.name}.migrate-",
                dir=plan.destination.parent,
            )
        ).resolve(strict=True)
        stage.chmod(0o700)

        for entry in plan.entries:
            source_path, _ = _validate_planned_source_entry(source, entry)
            relative_path = _safe_relative_path(entry.relative_path)
            target = stage / relative_path
            if entry.is_directory:
                _ensure_private_parents(stage, target.parent)
                if _lstat_if_present(target) is None:
                    target.mkdir(mode=0o700)
                elif not target.is_dir() or target.is_symlink():
                    raise FileExistsError(target)
                target.chmod(0o700)
                continue

            _ensure_private_parents(stage, target.parent)
            if _lstat_if_present(target) is not None:
                raise FileExistsError(target)
            if relative_path == Path("settings.json"):
                _validate_settings(source, source_path)
            shutil.copyfile(source_path, target)
            target.chmod(0o700 if entry.executable else 0o600)

        for generated in plan.generated_files:
            relative_path = _safe_relative_path(generated.relative_path)
            target = stage / relative_path
            _ensure_private_parents(stage, target.parent)
            with target.open("xb") as stream:
                stream.write(generated.body)
            target.chmod(0o700 if generated.executable else 0o600)

        _validate_destination(plan.destination)
        stage.rename(plan.destination)
        stage = None
        return plan.destination
    except BaseException:
        if stage is not None:
            _remove_known_stage(stage)
        raise


def render_migration_plan(plan: MigrationPlan) -> tuple[str, ...]:
    lines = []
    for entry in plan.entries:
        if entry.is_directory:
            continue
        kind = "executable" if entry.executable else "file"
        lines.append(f"COPY {kind} {entry.relative_path.as_posix()}")
    for generated in plan.generated_files:
        kind = "executable" if generated.executable else "file"
        lines.append(f"COPY {kind} {generated.relative_path.as_posix()}")
    lines.extend(f"SKIP denied {path.as_posix()}" for path in plan.skipped)
    lines.append(f"DESTINATION {plan.destination.as_posix()}")
    return tuple(lines)
