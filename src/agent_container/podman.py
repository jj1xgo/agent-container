from dataclasses import dataclass
import os
from pathlib import Path
import subprocess

from agent_container.state import Repository
from agent_container.state import StateLayout
from agent_container.state import validate_project_id


CODEX_STATUS_LINE_CONFIG = (
    'tui.status_line=["model-with-reasoning","context-remaining",'
    '"five-hour-limit","weekly-limit","git-branch","project-name"]'
)


_CLAUDE_TOKEN_PATH = "/run/secrets/claude-oauth-token"
_CLAUDE_LAUNCHER_PREFIX = (
    "python3",
    "-m",
    "agent_container.claude_launcher",
    _CLAUDE_TOKEN_PATH,
    "--",
    "claude",
)
_CLAUDE_CONFIG_TMPFS = "--tmpfs=/home/agent/.claude:rw,nosuid,nodev,noexec,size=16m"
_CLAUDE_RUNTIME_HOME_TMPFS_MOUNT = (
    "type=tmpfs,dst=/home/agent,tmpfs-size=16777216,"
    "tmpfs-mode=0700,U=true,noexec,nosuid,nodev"
)
_BROKER_RUNTIME_PATH = "/run/agent-broker"


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    environment: dict[str, str]


@dataclass(frozen=True)
class BrokerRuntimeMount:
    run_dir: Path
    repository: Repository


def _mount(source: Path, target: str, read_only: bool = False) -> str:
    options = f"type=bind,src={source},dst={target}"
    return f"{options},ro=true" if read_only else options


def _runtime_prefix(uid: int, gid: int) -> list[str]:
    return [
        "podman",
        "run",
        "--rm",
        "--interactive",
        "--tty",
        "--read-only",
        "--cap-drop=all",
        "--security-opt=no-new-privileges",
        "--userns=keep-id:uid=1000,gid=1000",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=512m",
    ]


def _noninteractive_prefix(uid: int, gid: int) -> list[str]:
    argv = _runtime_prefix(uid, gid)
    argv.remove("--interactive")
    argv.remove("--tty")
    return argv


def _git_environment_args(
    gh_config_dir: str = "/home/agent/.config/gh",
) -> list[str]:
    return [
        "--env",
        f"GH_CONFIG_DIR={gh_config_dir}",
        "--env",
        "GIT_CONFIG_COUNT=1",
        "--env",
        "GIT_CONFIG_KEY_0=credential.https://github.com.helper",
        "--env",
        "GIT_CONFIG_VALUE_0=!gh auth git-credential",
    ]


def _broker_git_args(
    layout: StateLayout, broker: BrokerRuntimeMount
) -> list[str]:
    if not broker.run_dir.is_absolute():
        raise ValueError("broker runtime path must be absolute")
    if broker.repository.name == "" or broker.repository.slug.count("/") != 1:
        raise ValueError("broker repository is invalid")
    broker_url = f"agent-broker://{broker.repository.slug}"
    github_url = f"https://github.com/{broker.repository.slug}"
    return [
        "--mount",
        _mount(broker.run_dir, _BROKER_RUNTIME_PATH, True),
        "--env",
        f"AGENT_BROKER_SOCKET={_BROKER_RUNTIME_PATH}/broker.sock",
        "--env",
        f"AGENT_BROKER_CAPABILITY={_BROKER_RUNTIME_PATH}/capability",
        "--env",
        f"AGENT_BROKER_REPOSITORY={broker.repository.slug}",
        "--env",
        f"AGENT_PROJECT_ID={layout.project_id}",
        "--env",
        "GIT_CONFIG_COUNT=1",
        "--env",
        f"GIT_CONFIG_KEY_0=url.{broker_url}.insteadOf",
        "--env",
        f"GIT_CONFIG_VALUE_0={github_url}",
    ]


def build_image_spec(
    repo_root: Path,
    image: str,
    node_version: str,
    codex_version: str,
    claude_version: str,
    cachebuster: str,
) -> CommandSpec:
    root = repo_root.resolve()
    return CommandSpec(
        (
            "podman",
            "build",
            "--build-arg",
            f"NODE_VERSION={node_version}",
            "--build-arg",
            f"CODEX_VERSION={codex_version}",
            "--build-arg",
            f"CLAUDE_VERSION={claude_version}",
            "--build-arg",
            f"AGENT_CLI_CACHEBUST={cachebuster}",
            "--tag",
            image,
            "--file",
            str(root / "Containerfile"),
            str(root),
        ),
        {},
    )


def podman_image_id_spec(image: str) -> CommandSpec:
    return CommandSpec(
        ("podman", "image", "inspect", "--format", "{{.Id}}", image), {}
    )


def podman_architecture_spec() -> CommandSpec:
    return CommandSpec(("podman", "info", "--format", "{{.Host.Arch}}"), {})


def podman_project_images_spec(project_id: str) -> CommandSpec:
    project_id = validate_project_id(project_id)
    reference = f"localhost/agent-container-project:{project_id}-*"
    return CommandSpec(
        (
            "podman",
            "images",
            "--filter",
            f"reference={reference}",
            "--format",
            "{{.Repository}}:{{.Tag}}",
        ),
        {},
    )


def build_project_image_spec(
    context: Path,
    containerfile: Path,
    base_image: str,
    image: str,
) -> CommandSpec:
    resolved_context = context.resolve()
    resolved_containerfile = containerfile.resolve()
    if resolved_containerfile.parent != resolved_context:
        raise ValueError("project Containerfile must be inside its build context")
    return CommandSpec(
        (
            "podman",
            "build",
            "--pull=never",
            "--build-arg",
            f"BASE_IMAGE={base_image}",
            "--tag",
            image,
            "--file",
            str(resolved_containerfile),
            str(resolved_context),
        ),
        {},
    )


def cli_version_spec(image: str, agent: str) -> CommandSpec:
    return CommandSpec(
        (
            "podman",
            "run",
            "--rm",
            "--read-only",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--userns=keep-id:uid=1000,gid=1000",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=512m",
            image,
            agent,
            "--version",
        ),
        {},
    )


def node_version_spec(image: str) -> CommandSpec:
    return _fixed_node_version_spec(image, "/opt/agent-node/bin/node")


def project_node_version_spec(image: str) -> CommandSpec:
    return _fixed_node_version_spec(image, "/opt/project-node/bin/node")


def _fixed_node_version_spec(image: str, executable: str) -> CommandSpec:
    return CommandSpec(
        (
            "podman",
            "run",
            "--rm",
            "--read-only",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--userns=keep-id:uid=1000,gid=1000",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=512m",
            image,
            executable,
            "--version",
        ),
        {},
    )


def claude_policy_status_spec(image: str) -> CommandSpec:
    return CommandSpec(
        (
            "podman",
            "run",
            "--rm",
            "--read-only",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--userns=keep-id:uid=1000,gid=1000",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=512m",
            image,
            "python3",
            "-m",
            "agent_container.claude_policy",
        ),
        {},
    )


def auth_codex_spec(layout: StateLayout, image: str) -> CommandSpec:
    argv = _runtime_prefix(os.getuid(), os.getgid())
    argv += ["--mount", _mount(layout.codex_auth_dir, "/home/agent/.codex")]
    argv += [image, "codex", "login", "--device-auth"]
    return CommandSpec(tuple(argv), {})


def codex_login_status_spec(layout: StateLayout, image: str) -> CommandSpec:
    argv = _runtime_prefix(os.getuid(), os.getgid())
    argv += ["--mount", _mount(layout.codex_auth_dir, "/home/agent/.codex")]
    argv += [image, "codex", "login", "status"]
    return CommandSpec(tuple(argv), {})


def _claude_setup_prefix() -> list[str]:
    argv = _runtime_prefix(os.getuid(), os.getgid())
    argv += [_CLAUDE_CONFIG_TMPFS]
    argv += ["--env", "CLAUDE_CONFIG_DIR=/home/agent/.claude"]
    return argv


def claude_setup_token_spec(image: str) -> CommandSpec:
    argv = _claude_setup_prefix()
    argv += [image, "claude", "setup-token"]
    return CommandSpec(tuple(argv), {})


def claude_token_status_spec(token_file: Path, image: str) -> CommandSpec:
    argv = _claude_setup_prefix()
    argv += ["--mount", _mount(token_file, _CLAUDE_TOKEN_PATH, True)]
    argv += [image, *_CLAUDE_LAUNCHER_PREFIX, "auth", "status"]
    return CommandSpec(tuple(argv), {})


def clone_project_spec(
    layout: StateLayout,
    repository: Repository,
    image: str,
    broker: BrokerRuntimeMount | None = None,
) -> CommandSpec:
    argv = _runtime_prefix(os.getuid(), os.getgid())
    if broker is None:
        argv += _git_environment_args()
        argv += ["--mount", _mount(layout.gh_dir, "/home/agent/.config/gh", True)]
    else:
        if broker.repository != repository:
            raise ValueError("broker repository does not match clone repository")
        argv += _broker_git_args(layout, broker)
    argv += ["--mount", _mount(layout.root / "workspaces", "/workspaces")]
    if broker is None:
        argv += [image, "gh", "repo", "clone", repository.slug]
    else:
        argv += [image, "git", "clone", repository.https_url]
    argv += [f"/workspaces/{layout.project_id}"]
    return CommandSpec(tuple(argv), {})


def codex_superpowers_marketplace_spec(
    layout: StateLayout, image: str, *, update: bool = False
) -> CommandSpec:
    argv = _noninteractive_prefix(os.getuid(), os.getgid())
    argv += ["--mount", _mount(layout.codex_home, "/home/agent/.codex")]
    argv += [image, "codex", "plugin", "marketplace"]
    if update:
        argv += ["upgrade", "superpowers-dev", "--json"]
    else:
        argv += ["add", "obra/superpowers", "--ref", "main", "--json"]
    return CommandSpec(tuple(argv), {})


def codex_superpowers_install_spec(layout: StateLayout, image: str) -> CommandSpec:
    argv = _noninteractive_prefix(os.getuid(), os.getgid())
    argv += ["--mount", _mount(layout.codex_home, "/home/agent/.codex")]
    argv += [
        image,
        "codex",
        "plugin",
        "add",
        "superpowers@superpowers-dev",
        "--json",
    ]
    return CommandSpec(tuple(argv), {})


def claude_superpowers_marketplace_spec(
    layout: StateLayout, image: str, *, update: bool = False
) -> CommandSpec:
    argv = _noninteractive_prefix(os.getuid(), os.getgid())
    argv += ["--mount", _CLAUDE_RUNTIME_HOME_TMPFS_MOUNT]
    argv += ["--mount", _mount(layout.claude_config, "/home/agent/.claude")]
    argv += ["--env", "CLAUDE_CONFIG_DIR=/home/agent/.claude"]
    action = "update" if update else "add"
    argv += [image, "claude", "plugin", "marketplace", action]
    if update:
        argv += ["claude-plugins-official"]
    else:
        argv += ["anthropics/claude-plugins-official"]
    return CommandSpec(tuple(argv), {})


def claude_superpowers_spec(
    layout: StateLayout, image: str, *, update: bool = False
) -> CommandSpec:
    argv = _noninteractive_prefix(os.getuid(), os.getgid())
    argv += ["--mount", _CLAUDE_RUNTIME_HOME_TMPFS_MOUNT]
    argv += ["--mount", _mount(layout.claude_config, "/home/agent/.claude")]
    argv += ["--env", "CLAUDE_CONFIG_DIR=/home/agent/.claude"]
    action = "update" if update else "install"
    argv += [
        image,
        "claude",
        "plugin",
        action,
        "superpowers@claude-plugins-official",
        "--scope",
        "user",
        "--yes",
    ]
    return CommandSpec(tuple(argv), {})


def run_codex_spec(
    layout: StateLayout,
    handover_project: Path,
    image: str,
    uid: int,
    gid: int,
    broker: BrokerRuntimeMount | None = None,
) -> CommandSpec:
    if uid != os.getuid() or gid != os.getgid():
        raise ValueError("runtime uid and gid must match the current user")
    argv = _runtime_prefix(uid, gid)
    argv += _git_environment_args() if broker is None else _broker_git_args(layout, broker)
    argv += ["--env", "AGENT_HANDOVER_ROOT=/handovers"]
    mounts = [
        (layout.workspace, "/workspace", False),
        (layout.codex_home, "/home/agent/.codex", False),
        (layout.codex_auth_file, "/home/agent/.codex/auth.json", False),
        (layout.cache, "/home/agent/.cache", False),
        (handover_project, f"/handovers/{layout.project_id}", False),
    ]
    if broker is None:
        mounts.insert(-1, (layout.gh_dir, "/home/agent/.config/gh", True))
    for source, target, read_only in mounts:
        argv += ["--mount", _mount(source, target, read_only)]
    argv += [
        "--env",
        f"AGENT_PROJECT_ID={layout.project_id}",
        image,
        "codex",
        "--approve-for-me",
        "-c",
        CODEX_STATUS_LINE_CONFIG,
    ]
    return CommandSpec(tuple(argv), {})


def run_claude_spec(
    layout: StateLayout,
    handover_project: Path,
    image: str,
    uid: int,
    gid: int,
    broker: BrokerRuntimeMount | None = None,
) -> CommandSpec:
    if uid != os.getuid() or gid != os.getgid():
        raise ValueError("runtime uid and gid must match the current user")
    gh_config_dir = "/home/agent/gh-config"
    argv = _runtime_prefix(uid, gid)
    argv += ["--mount", _CLAUDE_RUNTIME_HOME_TMPFS_MOUNT]
    argv += (
        _git_environment_args(gh_config_dir)
        if broker is None
        else _broker_git_args(layout, broker)
    )
    mounts = [
        (layout.workspace, "/workspace", False),
        (layout.claude_config, "/home/agent/.claude", False),
        (layout.claude_token_file, _CLAUDE_TOKEN_PATH, True),
        (layout.cache, "/home/agent/.cache", False),
        (handover_project, f"/handovers/{layout.project_id}", False),
    ]
    if broker is None:
        mounts.insert(-1, (layout.gh_dir, gh_config_dir, True))
    for source, target, read_only in mounts:
        argv += ["--mount", _mount(source, target, read_only)]
    argv += ["--env", "CLAUDE_CONFIG_DIR=/home/agent/.claude"]
    argv += ["--env", "AGENT_HANDOVER_ROOT=/handovers"]
    argv += ["--env", f"AGENT_PROJECT_ID={layout.project_id}", image]
    argv += _CLAUDE_LAUNCHER_PREFIX
    return CommandSpec(tuple(argv), {})


def podman_version_spec() -> CommandSpec:
    return CommandSpec(("podman", "--version"), {})


def podman_rootless_spec() -> CommandSpec:
    return CommandSpec(
        ("podman", "info", "--format", "{{.Host.Security.Rootless}}"), {}
    )


def podman_image_exists_spec(image: str) -> CommandSpec:
    return CommandSpec(("podman", "image", "exists", image), {})


def run_command(
    spec: CommandSpec,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(spec.environment)
    return subprocess.run(
        spec.argv,
        env=environment,
        text=True,
        check=check,
        capture_output=capture_output,
    )
