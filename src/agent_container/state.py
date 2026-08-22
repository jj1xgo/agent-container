from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping


PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
REPOSITORY_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def validate_project_id(value: str) -> str:
    if value in {".", ".."} or PROJECT_ID.fullmatch(value) is None:
        raise ValueError("project_id must be a single safe repository-style slug")
    return value


@dataclass(frozen=True)
class Repository:
    owner: str
    name: str

    @classmethod
    def parse(cls, value: str) -> "Repository":
        parts = value.split("/")
        if len(parts) != 2 or any(
            part in {"", ".", ".."} or REPOSITORY_PART.fullmatch(part) is None
            for part in parts
        ):
            raise ValueError("repository must be OWNER/REPOSITORY")
        return cls(*parts)

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def https_url(self) -> str:
        return f"https://github.com/{self.slug}.git"


@dataclass(frozen=True)
class StateLayout:
    root: Path
    project_id: str

    @classmethod
    def from_environment(
        cls, project_id: str, environment: Mapping[str, str] | None = None
    ) -> "StateLayout":
        env = os.environ if environment is None else environment
        if env.get("AGENT_CONTAINER_HOME"):
            root = Path(env["AGENT_CONTAINER_HOME"])
        elif env.get("XDG_DATA_HOME"):
            root = Path(env["XDG_DATA_HOME"]) / "agent-container"
        else:
            root = Path.home() / ".local/share/agent-container"
        if not root.is_absolute():
            raise ValueError("agent container state root must be absolute")
        return cls(root.resolve(), validate_project_id(project_id))

    @property
    def gh_dir(self) -> Path:
        return self.root / "gh"

    @property
    def codex_auth_dir(self) -> Path:
        return self.root / "shared-auth/codex"

    @property
    def codex_auth_file(self) -> Path:
        return self.codex_auth_dir / "auth.json"

    @property
    def project_dir(self) -> Path:
        return self.root / "projects" / self.project_id

    @property
    def codex_home(self) -> Path:
        return self.project_dir / "codex-home"

    @property
    def cache(self) -> Path:
        return self.project_dir / "cache"

    @property
    def project_file(self) -> Path:
        return self.project_dir / "project.json"

    @property
    def workspace(self) -> Path:
        return self.root / "workspaces" / self.project_id


def ensure_private_directory(path: Path, create: bool = False) -> Path:
    if path.is_symlink():
        raise ValueError(f"directory must not be a symlink: {path}")
    if create and not path.exists():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise FileNotFoundError(path)
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise PermissionError(f"directory must have mode 0700: {path}")
    if path.stat().st_uid != os.getuid():
        raise PermissionError(f"directory must be owned by the current user: {path}")
    return path.resolve()


def ensure_private_file(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f"credential file must not be a symlink: {path}")
    if not path.is_file():
        raise FileNotFoundError(path)
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise PermissionError(f"credential file must have mode 0600: {path}")
    if path.stat().st_uid != os.getuid():
        raise PermissionError(f"credential file must be owned by the current user: {path}")
    return path.resolve()


def validate_workspace_origin(
    workspace: Path, repository: Repository, remote_url: str
) -> None:
    if workspace.is_symlink() or not (workspace / ".git").is_dir():
        raise ValueError(f"workspace is not a safe Git repository: {workspace}")
    allowed = {
        repository.https_url,
        repository.https_url.removesuffix(".git"),
    }
    if remote_url.strip() not in allowed:
        raise ValueError(
            f"workspace origin does not match {repository.slug}: {workspace}"
        )


@dataclass(frozen=True)
class ProjectRecord:
    repository: Repository
    handover_root: Path

    def write(self, path: Path) -> None:
        payload = {
            "repository": self.repository.slug,
            "handover_root": str(self.handover_root),
        }
        with path.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        path.chmod(0o600)

    @classmethod
    def read(cls, path: Path) -> "ProjectRecord":
        ensure_private_file(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if set(payload) != {"repository", "handover_root"}:
            raise ValueError("project metadata has unexpected fields")
        handover_root = Path(payload["handover_root"])
        if not handover_root.is_absolute():
            raise ValueError("handover_root must be absolute")
        return cls(Repository.parse(payload["repository"]), handover_root.resolve())
