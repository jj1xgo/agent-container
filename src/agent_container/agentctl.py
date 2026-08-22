import argparse
from pathlib import Path
import subprocess
import sys
from typing import Callable
from typing import Mapping
from typing import TextIO

from agent_container.podman import CommandSpec
from agent_container.podman import auth_codex_spec
from agent_container.podman import build_image_spec
from agent_container.podman import clone_project_spec
from agent_container.podman import run_command
from agent_container.profile import seed_codex_home
from agent_container.state import ProjectRecord
from agent_container.state import Repository
from agent_container.state import StateLayout
from agent_container.state import ensure_private_directory
from agent_container.state import ensure_private_file
from agent_container.state import validate_workspace_origin


DEFAULT_IMAGE = "localhost/agent-container:dev"


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="agentctl")
    command.add_argument("--image", default=DEFAULT_IMAGE)
    subcommands = command.add_subparsers(dest="command", required=True)
    subcommands.add_parser("build")
    auth = subcommands.add_parser("auth")
    auth.add_subparsers(dest="agent", required=True).add_parser("codex")
    project = subcommands.add_parser("project")
    project_subcommands = project.add_subparsers(dest="project_command", required=True)
    add = project_subcommands.add_parser("add")
    add.add_argument("repository")
    add.add_argument("--project")
    add.add_argument("--handover-root", type=Path, required=True)
    run = subcommands.add_parser("run")
    run.add_argument("project")
    doctor = subcommands.add_parser("doctor")
    doctor.add_argument("project")
    return command


def read_git_remote(workspace: Path) -> str:
    completed = subprocess.run(
        ("git", "-C", str(workspace), "remote", "get-url", "origin"),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _seed_shared_auth_home(layout: StateLayout, profile_root: Path) -> None:
    managed_paths = ("config.toml", "hooks.json", "skills")
    if not any(
        (layout.codex_auth_dir / path).exists()
        or (layout.codex_auth_dir / path).is_symlink()
        for path in managed_paths
    ):
        seed_codex_home(profile_root, layout.codex_auth_dir)


def _prepare_codex_auth(layout: StateLayout, profile_root: Path) -> None:
    ensure_private_directory(layout.root, create=True)
    ensure_private_directory(layout.root / "shared-auth", create=True)
    if layout.codex_auth_dir.exists() or layout.codex_auth_dir.is_symlink():
        ensure_private_directory(layout.codex_auth_dir)
        _seed_shared_auth_home(layout, profile_root)
    else:
        seed_codex_home(profile_root, layout.codex_auth_dir)
        ensure_private_directory(layout.codex_auth_dir)
    if layout.codex_auth_file.exists() or layout.codex_auth_file.is_symlink():
        ensure_private_file(layout.codex_auth_file)


def _resolve_handover_root(handover_root: Path, project_id: str) -> Path:
    if not handover_root.is_absolute():
        raise ValueError("handover root must be absolute")
    if handover_root.is_symlink():
        raise ValueError(f"handover root must not be a symlink: {handover_root}")
    if not handover_root.is_dir():
        raise FileNotFoundError(handover_root)
    handover_project = handover_root / project_id
    if handover_project.is_symlink():
        raise ValueError(f"handover project must not be a symlink: {handover_project}")
    if not handover_project.is_dir():
        raise FileNotFoundError(handover_project)
    return handover_root.resolve()


def _prepare_project_directories(layout: StateLayout) -> None:
    ensure_private_directory(layout.root, create=True)
    ensure_private_directory(layout.gh_dir, create=True)
    ensure_private_directory(layout.project_dir.parent, create=True)
    ensure_private_directory(layout.project_dir, create=True)
    ensure_private_directory(layout.cache, create=True)
    ensure_private_directory(layout.workspace.parent, create=True)


def _seed_project_codex_home(layout: StateLayout, profile_root: Path) -> None:
    if layout.codex_home.exists() or layout.codex_home.is_symlink():
        ensure_private_directory(layout.codex_home)
        if any(layout.codex_home.iterdir()):
            return
    seed_codex_home(profile_root, layout.codex_home)
    ensure_private_directory(layout.codex_home)


def _add_project(
    repository_value: str,
    project_value: str | None,
    handover_root: Path,
    image: str,
    environment: Mapping[str, str] | None,
    runner: Callable[[CommandSpec], subprocess.CompletedProcess],
    git_remote_reader: Callable[[Path], str],
    profile_root: Path,
) -> None:
    repository = Repository.parse(repository_value)
    project_id = project_value if project_value is not None else repository.name
    layout = StateLayout.from_environment(project_id, environment)
    resolved_handover_root = _resolve_handover_root(handover_root, layout.project_id)
    _prepare_project_directories(layout)
    ensure_private_file(layout.gh_dir / "hosts.yml")
    if not (layout.workspace.exists() or layout.workspace.is_symlink()):
        runner(clone_project_spec(layout, repository, image))
    validate_workspace_origin(
        layout.workspace, repository, git_remote_reader(layout.workspace)
    )
    _seed_project_codex_home(layout, profile_root)
    ProjectRecord(repository, resolved_handover_root).write(layout.project_file)


def main(
    argv: list[str] | None = None,
    environment: Mapping[str, str] | None = None,
    runner: Callable[[CommandSpec], subprocess.CompletedProcess] = run_command,
    git_remote_reader: Callable[[Path], str] = read_git_remote,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    del stdout
    try:
        arguments = parser().parse_args(argv)
        repository_root = Path(__file__).resolve().parents[2]
        if arguments.command == "build":
            runner(build_image_spec(repository_root, arguments.image))
            return 0
        if arguments.command == "auth" and arguments.agent == "codex":
            layout = StateLayout.from_environment("auth", environment)
            _prepare_codex_auth(layout, repository_root / "profiles/codex")
            runner(auth_codex_spec(layout, arguments.image))
            ensure_private_file(layout.codex_auth_file)
            return 0
        if arguments.command == "project" and arguments.project_command == "add":
            _add_project(
                arguments.repository,
                arguments.project,
                arguments.handover_root,
                arguments.image,
                environment,
                runner,
                git_remote_reader,
                repository_root / "profiles/codex",
            )
            return 0
        return 1
    except subprocess.CalledProcessError as error:
        print(f"error: command failed with exit code {error.returncode}", file=stderr)
        return error.returncode or 1
    except (ValueError, PermissionError, FileNotFoundError) as error:
        print(f"error: {error}", file=stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
