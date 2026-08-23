from dataclasses import dataclass
import os
from pathlib import Path
import subprocess

from agent_container.state import Repository
from agent_container.state import StateLayout


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    environment: dict[str, str]


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


def _git_environment_args() -> list[str]:
    return [
        "--env",
        "GH_CONFIG_DIR=/home/agent/.config/gh",
        "--env",
        "GIT_CONFIG_COUNT=1",
        "--env",
        "GIT_CONFIG_KEY_0=credential.https://github.com.helper",
        "--env",
        "GIT_CONFIG_VALUE_0=!gh auth git-credential",
    ]


def build_image_spec(
    repo_root: Path,
    image: str,
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


def cli_version_spec(image: str, agent: str) -> CommandSpec:
    return CommandSpec(
        (
            "podman",
            "run",
            "--rm",
            "--read-only",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            image,
            agent,
            "--version",
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


def clone_project_spec(
    layout: StateLayout, repository: Repository, image: str
) -> CommandSpec:
    argv = _runtime_prefix(os.getuid(), os.getgid())
    argv += _git_environment_args()
    argv += ["--mount", _mount(layout.gh_dir, "/home/agent/.config/gh", True)]
    argv += ["--mount", _mount(layout.root / "workspaces", "/workspaces")]
    argv += [
        image,
        "gh",
        "repo",
        "clone",
        repository.slug,
        f"/workspaces/{layout.project_id}",
    ]
    return CommandSpec(tuple(argv), {})


def run_codex_spec(
    layout: StateLayout,
    handover_project: Path,
    image: str,
    uid: int,
    gid: int,
) -> CommandSpec:
    if uid != os.getuid() or gid != os.getgid():
        raise ValueError("runtime uid and gid must match the current user")
    argv = _runtime_prefix(uid, gid)
    argv += _git_environment_args()
    argv += ["--env", "AGENT_HANDOVER_ROOT=/handovers"]
    mounts = (
        (layout.workspace, "/workspace", False),
        (layout.codex_home, "/home/agent/.codex", False),
        (layout.codex_auth_file, "/home/agent/.codex/auth.json", False),
        (layout.cache, "/home/agent/.cache", False),
        (layout.gh_dir, "/home/agent/.config/gh", True),
        (handover_project, f"/handovers/{layout.project_id}", False),
    )
    for source, target, read_only in mounts:
        argv += ["--mount", _mount(source, target, read_only)]
    argv += ["--env", f"AGENT_PROJECT_ID={layout.project_id}", image, "codex"]
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
