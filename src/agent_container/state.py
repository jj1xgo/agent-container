from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping


PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
REPOSITORY_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
AGENTS = frozenset({"codex", "claude"})
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,99}$")
PLUGIN_IDENTIFIER = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}@[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
)


def validate_agent(value: str, allow_all: bool = False) -> str:
    allowed = AGENTS | ({"all"} if allow_all else set())
    if value not in allowed:
        raise ValueError("agent must be codex or claude")
    return value


def validate_version(value: str) -> str:
    if VERSION.fullmatch(value) is None:
        raise ValueError("version must be a safe npm version or latest")
    return value


def validate_plugin_identifier(value: str) -> str:
    if PLUGIN_IDENTIFIER.fullmatch(value) is None:
        raise ValueError("plugin must be NAME@MARKETPLACE")
    return value


def validate_project_id(value: str) -> str:
    if value in {".", ".."} or PROJECT_ID.fullmatch(value) is None:
        raise ValueError("project_id must be a single safe repository-style slug")
    return value


def github_broker_project_label(project_id: str) -> str:
    validated = validate_project_id(project_id)
    return hashlib.sha256(validated.encode("ascii")).hexdigest()[:12]


def handover_broker_project_label(project_id: str) -> str:
    validated = validate_project_id(project_id)
    return hashlib.sha256(validated.encode("ascii")).hexdigest()[:12]


def egress_broker_project_label(project_id: str) -> str:
    validated = validate_project_id(project_id)
    return hashlib.sha256(validated.encode("ascii")).hexdigest()[:12]


def validate_claude_oauth_token(value: str) -> str:
    if not 32 <= len(value) <= 4096 or any(
        ord(character) < 33 or ord(character) > 126 for character in value
    ):
        raise ValueError("Claude OAuth token has invalid format")
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
    def claude_auth_dir(self) -> Path:
        return self.root / "shared-auth/claude"

    @property
    def claude_token_file(self) -> Path:
        return self.claude_auth_dir / "oauth-token"

    @property
    def claude_legacy_credentials_file(self) -> Path:
        return self.claude_auth_dir / ".credentials.json"

    @property
    def claude_legacy_metadata_file(self) -> Path:
        return self.claude_auth_dir / ".claude.json"

    @property
    def claude_legacy_backups(self) -> Path:
        return self.claude_auth_dir / "backups"

    @property
    def claude_quarantine_root(self) -> Path:
        return self.root / "quarantine/claude"

    @property
    def project_dir(self) -> Path:
        return self.root / "projects" / self.project_id

    @property
    def codex_home(self) -> Path:
        return self.project_dir / "codex-home"

    @property
    def claude_config(self) -> Path:
        return self.project_dir / "claude-config"

    @property
    def cache(self) -> Path:
        return self.project_dir / "cache"

    @property
    def project_file(self) -> Path:
        return self.project_dir / "project.json"

    @property
    def workspace(self) -> Path:
        return self.root / "workspaces" / self.project_id

    @property
    def github_broker_root(self) -> Path:
        return self.root / "github-broker"

    @property
    def github_broker_run_root(self) -> Path:
        return (
            self.github_broker_root
            / "r"
            / github_broker_project_label(self.project_id)
        )

    @property
    def github_broker_policy_file(self) -> Path:
        return self.project_dir / "github-broker.json"

    @property
    def handover_broker_root(self) -> Path:
        return self.root / "handover-broker"

    @property
    def handover_broker_run_root(self) -> Path:
        return (
            self.handover_broker_root
            / "r"
            / handover_broker_project_label(self.project_id)
        )

    @property
    def handover_broker_audit_file(self) -> Path:
        return self.handover_broker_root / "audit/events.jsonl"

    @property
    def egress_policy_file(self) -> Path:
        return self.project_dir / "egress.json"

    @property
    def egress_broker_root(self) -> Path:
        return self.root / "egress-broker"

    @property
    def egress_broker_run_root(self) -> Path:
        return (
            self.egress_broker_root
            / "r"
            / egress_broker_project_label(self.project_id)
        )

    @property
    def egress_broker_audit_file(self) -> Path:
        return self.egress_broker_root / "audit/events.jsonl"

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


def validate_workspace(workspace: Path) -> None:
    git_dir = workspace / ".git"
    if (
        not workspace.is_dir()
        or workspace.is_symlink()
        or git_dir.is_symlink()
        or not git_dir.is_dir()
    ):
        raise ValueError(f"workspace is not a safe Git repository: {workspace}")


def validate_workspace_origin(
    workspace: Path, repository: Repository, remote_url: str
) -> None:
    validate_workspace(workspace)
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
