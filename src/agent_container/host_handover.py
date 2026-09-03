from collections.abc import Callable
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from agent_container.handover_writer import create_atomic_handover
from agent_container.handover_broker_protocol import MAX_DOCUMENT_BYTES
from agent_container.state import ensure_private_file
from agent_container.state import Repository
from agent_container.state import validate_project_id


RemoteGetter = Callable[[Path], str]
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _normalize_github_remote(value: str) -> str:
    remote = value.strip()
    prefixes = (
        "https://github.com/",
        "ssh://git@github.com/",
        "git@github.com:",
    )
    for prefix in prefixes:
        if remote.startswith(prefix):
            return remote.removeprefix(prefix).removesuffix(".git")
    raise ValueError("workspace origin is not a supported GitHub remote")


def _git_remote(cwd: Path) -> str:
    completed = subprocess.run(
        ("git", "-C", str(cwd), "remote", "get-url", "origin"),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ValueError("workspace origin is unavailable")
    return completed.stdout


def _read_private_body(path: Path) -> str:
    temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
    if (
        not path.is_absolute()
        or not path.name.startswith("agent-handover-")
        or not path.name.endswith(".md")
        or path.parent.resolve(strict=True) != temporary_root
    ):
        raise ValueError("handover body file is invalid")
    descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW)
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_uid != os.getuid()
        ):
            raise ValueError("handover body file is invalid")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            body = stream.read(MAX_DOCUMENT_BYTES + 1)
        if len(body) > MAX_DOCUMENT_BYTES:
            raise ValueError("handover body file is invalid")
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("handover body file is invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_registration(
    projects_root: Path, project_id: str
) -> tuple[str, Path]:
    metadata = ensure_private_file(
        projects_root / validate_project_id(project_id) / "project.json"
    )

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("project metadata contains duplicate fields")
            result[key] = value
        return result

    payload = json.loads(
        metadata.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates
    )
    if not isinstance(payload, dict) or set(payload) != {
        "repository",
        "handover_root",
    }:
        raise ValueError("project metadata has unexpected fields")
    if not isinstance(payload["repository"], str) or not isinstance(
        payload["handover_root"], str
    ):
        raise ValueError("project metadata has invalid fields")
    repository = Repository.parse(payload["repository"]).slug
    handover_root = Path(payload["handover_root"])
    if (
        not handover_root.is_absolute()
        or ".." in handover_root.parts
        or handover_root.is_symlink()
        or handover_root.resolve(strict=True) != handover_root
        or not handover_root.is_dir()
    ):
        raise ValueError("registered handover directory is invalid")
    project_dir = handover_root / project_id
    if (
        project_dir.is_symlink()
        or project_dir.resolve(strict=True) != project_dir
        or not project_dir.is_dir()
    ):
        raise ValueError("registered handover directory is invalid")
    return repository, project_dir


def _registered_target(
    cwd: Path,
    projects_root: Path,
    repository: str,
) -> tuple[str, Path]:
    preferred_id = validate_project_id(cwd.name)
    preferred_metadata = projects_root / preferred_id / "project.json"
    if preferred_metadata.exists():
        registered_repository, project_dir = _read_registration(
            projects_root, preferred_id
        )
        if registered_repository == repository:
            return preferred_id, project_dir

    matches: list[tuple[str, Path]] = []
    for entry in sorted(projects_root.iterdir(), key=lambda path: path.name):
        if not entry.is_dir() or entry.is_symlink():
            continue
        try:
            project_id = validate_project_id(entry.name)
            registered_repository, project_dir = _read_registration(
                projects_root, project_id
            )
        except (FileNotFoundError, PermissionError, ValueError, OSError):
            continue
        if registered_repository == repository:
            matches.append((project_id, project_dir))
    if len(matches) != 1:
        raise ValueError("workspace origin must match one unique registered project")
    return matches[0]


def publish_host_handover(
    *,
    cwd: Path,
    projects_root: Path,
    title: str,
    body_file: Path,
    session_id: str = "",
    remote_getter: RemoteGetter = _git_remote,
) -> Path:
    workspace = cwd.resolve(strict=True)
    if not workspace.is_dir() or workspace.is_symlink():
        raise ValueError("workspace is invalid")
    repository = _normalize_github_remote(remote_getter(workspace))
    project_id, project_dir = _registered_target(
        workspace,
        projects_root.resolve(strict=True),
        repository,
    )
    body = _read_private_body(body_file)
    return create_atomic_handover(
        project_dir,
        project_id,
        title,
        body,
        session_id=session_id,
    )
