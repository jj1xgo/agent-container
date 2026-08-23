import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable
from typing import Mapping
from typing import TextIO

from agent_container.podman import CommandSpec
from agent_container.podman import auth_codex_spec
from agent_container.podman import auth_claude_spec
from agent_container.podman import build_image_spec
from agent_container.podman import clone_project_spec
from agent_container.podman import cli_version_spec
from agent_container.podman import codex_login_status_spec
from agent_container.podman import claude_login_status_spec
from agent_container.podman import podman_image_exists_spec
from agent_container.podman import podman_rootless_spec
from agent_container.podman import podman_version_spec
from agent_container.podman import run_codex_spec
from agent_container.podman import run_claude_spec
from agent_container.podman import run_command
from agent_container.profile import seed_codex_home
from agent_container.state import ProjectRecord
from agent_container.state import Repository
from agent_container.state import StateLayout
from agent_container.state import ensure_private_directory
from agent_container.state import ensure_private_file
from agent_container.state import validate_workspace
from agent_container.state import validate_workspace_origin
from agent_container.state import validate_agent
from agent_container.state import validate_plugin_identifier
from agent_container.state import validate_version


DEFAULT_IMAGE = "localhost/agent-container:dev"
RuntimeSpecBuilder = Callable[[StateLayout, Path, str, int, int], CommandSpec]


def read_cachebuster() -> str:
    return str(time.time_ns())


@dataclass(frozen=True)
class CheckResult:
    level: str
    name: str
    detail: str


def _validate_image(value: str) -> str:
    if (
        not value
        or value.startswith("-")
        or any(
            character.isspace()
            or ord(character) < 32
            or ord(character) == 127
            for character in value
        )
    ):
        raise ValueError("image must be a non-empty name without whitespace or options")
    return value


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="agentctl")
    command.add_argument("--image", default=DEFAULT_IMAGE)
    subcommands = command.add_subparsers(dest="command", required=True)
    build = subcommands.add_parser("build")
    build.add_argument("--codex-version", default="latest")
    build.add_argument("--claude-version", default="latest")
    auth = subcommands.add_parser("auth")
    auth_subcommands = auth.add_subparsers(dest="agent", required=True)
    auth_subcommands.add_parser("codex")
    auth_subcommands.add_parser("claude")
    project = subcommands.add_parser("project")
    project_subcommands = project.add_subparsers(dest="project_command", required=True)
    add = project_subcommands.add_parser("add")
    add.add_argument("repository")
    add.add_argument("--project")
    add.add_argument("--handover-root", type=Path, required=True)
    run = subcommands.add_parser("run")
    run.add_argument("project")
    run.add_argument("--agent", choices=("codex", "claude"), default="codex")
    doctor = subcommands.add_parser("doctor")
    doctor.add_argument("project")
    doctor.add_argument("--agent", choices=("codex", "claude", "all"), default="codex")
    migrate = subcommands.add_parser("migrate")
    migrate.add_argument("agent", choices=("claude",))
    migrate.add_argument("project")
    migrate.add_argument("--from", dest="source", type=Path, required=True)
    migrate.add_argument("--plugin", dest="plugins", action="append", default=[])
    migrate.add_argument("--apply", action="store_true")
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


def _validate_existing_codex_auth(layout: StateLayout) -> None:
    for directory in (
        layout.root,
        layout.root / "shared-auth",
        layout.codex_auth_dir,
    ):
        if directory.exists() or directory.is_symlink():
            ensure_private_directory(directory)
    if layout.codex_auth_file.exists() or layout.codex_auth_file.is_symlink():
        ensure_private_file(layout.codex_auth_file)


def _prepare_claude_auth(layout: StateLayout) -> None:
    ensure_private_directory(layout.root, create=True)
    ensure_private_directory(layout.root / "shared-auth", create=True)
    ensure_private_directory(layout.claude_auth_dir, create=True)
    if layout.claude_auth_file.exists() or layout.claude_auth_file.is_symlink():
        ensure_private_file(layout.claude_auth_file)


def _validate_existing_claude_auth(layout: StateLayout) -> None:
    for directory in (
        layout.root,
        layout.root / "shared-auth",
        layout.claude_auth_dir,
    ):
        if directory.exists() or directory.is_symlink():
            ensure_private_directory(directory)
    if layout.claude_auth_file.exists() or layout.claude_auth_file.is_symlink():
        ensure_private_file(layout.claude_auth_file)


def _resolve_handover_root(handover_root: Path, project_id: str) -> Path:
    if not handover_root.is_absolute():
        raise ValueError("handover root must be absolute")
    if handover_root.is_symlink():
        raise ValueError(f"handover root must not be a symlink: {handover_root}")
    if not handover_root.is_dir():
        raise FileNotFoundError(handover_root)
    resolved_root = handover_root.resolve(strict=True)
    if resolved_root != handover_root:
        raise ValueError(
            f"handover root must not contain symlinks or traversal: {handover_root}"
        )
    handover_project = handover_root / project_id
    if handover_project.is_symlink():
        raise ValueError(f"handover project must not be a symlink: {handover_project}")
    if not handover_project.is_dir():
        raise FileNotFoundError(handover_project)
    resolved_project = handover_project.resolve(strict=True)
    if resolved_project.parent != resolved_root:
        raise ValueError("handover project must stay within its configured root")
    return resolved_root


def _configured_state_root(
    environment: Mapping[str, str] | None,
) -> Path:
    env = os.environ if environment is None else environment
    if env.get("AGENT_CONTAINER_HOME"):
        root = Path(env["AGENT_CONTAINER_HOME"])
    elif env.get("XDG_DATA_HOME"):
        root = Path(env["XDG_DATA_HOME"]) / "agent-container"
    else:
        root = Path.home() / ".local/share/agent-container"
    if not root.is_absolute():
        raise ValueError("agent container state root must be absolute")
    return root


def _ensure_exact_state_root(
    layout: StateLayout,
    environment: Mapping[str, str] | None,
) -> None:
    configured_root = _configured_state_root(environment)
    if configured_root.is_symlink() or configured_root != layout.root:
        raise ValueError(
            "agent container state root must not contain symlinks or traversal"
        )


def _prepare_project_directories(layout: StateLayout) -> None:
    ensure_private_directory(layout.root, create=True)
    ensure_private_directory(layout.gh_dir, create=True)
    ensure_private_directory(layout.project_dir.parent, create=True)
    ensure_private_directory(layout.project_dir, create=True)
    ensure_private_directory(layout.cache, create=True)
    ensure_private_directory(layout.workspace.parent, create=True)


def _prepare_claude_project_state(layout: StateLayout) -> None:
    ensure_private_directory(layout.claude_config, create=True)


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
    _ensure_exact_state_root(layout, environment)
    workspace_exists = layout.workspace.exists() or layout.workspace.is_symlink()
    if workspace_exists:
        validate_workspace(layout.workspace)
        validate_workspace_origin(
            layout.workspace, repository, git_remote_reader(layout.workspace)
        )
    _podman_preflight(runner, image_required=image)
    _prepare_project_directories(layout)
    ensure_private_file(layout.gh_dir / "hosts.yml")
    if not workspace_exists:
        runner(clone_project_spec(layout, repository, image))
        validate_workspace(layout.workspace)
        validate_workspace_origin(
            layout.workspace, repository, git_remote_reader(layout.workspace)
        )
    _seed_project_codex_home(layout, profile_root)
    ProjectRecord(repository, resolved_handover_root).write(layout.project_file)


def _runtime_preflight(
    project_id: str,
    agent: str,
    environment: Mapping[str, str] | None,
    git_remote_reader: Callable[[Path], str],
    identity_reader: Callable[[], tuple[int, int]],
) -> tuple[StateLayout, Path, int, int]:
    layout = StateLayout.from_environment(project_id, environment)
    _ensure_exact_state_root(layout, environment)
    for directory in _common_runtime_state_directories(layout):
        ensure_private_directory(directory)
    _validate_runtime_agent_state(layout, agent)
    ensure_private_file(layout.gh_dir / "hosts.yml")
    record = _read_runtime_project(layout.project_file)
    validate_workspace(layout.workspace)
    validate_workspace_origin(
        layout.workspace,
        record.repository,
        git_remote_reader(layout.workspace),
    )
    handover_root = _resolve_handover_root(record.handover_root, layout.project_id)
    handover_project = handover_root / layout.project_id
    uid, gid = _validated_process_identity(identity_reader)
    return layout, handover_project, uid, gid


def read_process_identity() -> tuple[int, int]:
    return os.getuid(), os.getgid()


def _validated_process_identity(
    identity_reader: Callable[[], tuple[int, int]],
) -> tuple[int, int]:
    identity = identity_reader()
    if (
        not isinstance(identity, tuple)
        or len(identity) != 2
        or not all(isinstance(value, int) for value in identity)
        or identity != (os.getuid(), os.getgid())
    ):
        raise ValueError("runtime uid and gid must match the current process")
    return identity


def _common_runtime_state_directories(layout: StateLayout) -> tuple[Path, ...]:
    return (
        layout.root,
        layout.root / "shared-auth",
        layout.gh_dir,
        layout.project_dir.parent,
        layout.project_dir,
        layout.cache,
        layout.workspace.parent,
    )


def _runtime_agent_directories(layout: StateLayout, agent: str) -> tuple[Path, ...]:
    if agent == "codex":
        return (layout.codex_auth_dir, layout.codex_home)
    if agent == "claude":
        directories = [layout.claude_auth_dir]
        if layout.claude_config.exists() or layout.claude_config.is_symlink():
            directories.append(layout.claude_config)
        return tuple(directories)
    raise ValueError("agent must be codex or claude")


def _runtime_agent_auth_file(layout: StateLayout, agent: str) -> Path:
    if agent == "codex":
        return layout.codex_auth_file
    if agent == "claude":
        return layout.claude_auth_file
    raise ValueError("agent must be codex or claude")


def _validate_runtime_agent_state(layout: StateLayout, agent: str) -> None:
    for directory in _runtime_agent_directories(layout, agent):
        ensure_private_directory(directory)
    ensure_private_file(_runtime_agent_auth_file(layout, agent))


def _private_state_directories(layout: StateLayout) -> tuple[Path, ...]:
    return _common_runtime_state_directories(layout) + _runtime_agent_directories(
        layout, "codex"
    )


def _read_runtime_project(path: Path) -> ProjectRecord:
    ensure_private_file(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or set(payload) != {"repository", "handover_root"}
        or not isinstance(payload["repository"], str)
        or not isinstance(payload["handover_root"], str)
    ):
        raise ValueError("project metadata has invalid fields")
    record = ProjectRecord.read(path)
    configured_handover_root = Path(payload["handover_root"])
    if configured_handover_root != record.handover_root:
        raise ValueError(
            "handover root in project metadata must not contain symlinks or traversal"
        )
    return record


def _doctor_run(
    runner: Callable[[CommandSpec], subprocess.CompletedProcess],
    spec: CommandSpec,
) -> subprocess.CompletedProcess:
    try:
        if runner is run_command:
            return run_command(spec, check=False, capture_output=True)
        return runner(spec)
    except subprocess.CalledProcessError as error:
        return subprocess.CompletedProcess(
            spec.argv,
            error.returncode,
            stdout=error.stdout,
            stderr=error.stderr,
        )
    except OSError:
        return subprocess.CompletedProcess(spec.argv, 1)


def _required_probe_run(
    runner: Callable[[CommandSpec], subprocess.CompletedProcess],
    spec: CommandSpec,
) -> subprocess.CompletedProcess:
    if runner is run_command:
        completed = run_command(spec, check=False, capture_output=True)
    else:
        completed = runner(spec)
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(completed.returncode, spec.argv)
    return completed


def _require_success(
    completed: subprocess.CompletedProcess,
    spec: CommandSpec,
) -> None:
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(completed.returncode, spec.argv)


def _podman_preflight(
    runner: Callable[[CommandSpec], subprocess.CompletedProcess],
    image_required: str | None = None,
) -> None:
    _required_probe_run(runner, podman_version_spec())
    rootless = _required_probe_run(runner, podman_rootless_spec())
    if (rootless.stdout or "").strip().lower() != "true":
        raise ValueError("rootless Podman is required")
    if image_required is not None:
        _required_probe_run(runner, podman_image_exists_spec(image_required))


def _check_failure_detail(error: Exception) -> str:
    if isinstance(error, OSError) and not isinstance(
        error, (PermissionError, FileNotFoundError)
    ):
        return "filesystem operation failed"
    return str(error)


def _doctor(
    project_id: str,
    image: str,
    environment: Mapping[str, str] | None,
    runner: Callable[[CommandSpec], subprocess.CompletedProcess],
    git_remote_reader: Callable[[Path], str],
) -> list[CheckResult]:
    layout = StateLayout.from_environment(project_id, environment)
    checks: list[CheckResult] = []

    version = _doctor_run(runner, podman_version_spec())
    checks.append(
        CheckResult(
            "PASS" if version.returncode == 0 else "FAIL",
            "podman-version",
            "available" if version.returncode == 0 else "unavailable",
        )
    )

    rootless = _doctor_run(runner, podman_rootless_spec())
    rootless_value = (rootless.stdout or "").strip().lower()
    rootless_ok = rootless.returncode == 0 and rootless_value == "true"
    checks.append(
        CheckResult(
            "PASS" if rootless_ok else "FAIL",
            "podman-rootless",
            "true" if rootless_ok else "rootless mode is required",
        )
    )

    image_check = _doctor_run(runner, podman_image_exists_spec(image))
    checks.append(
        CheckResult(
            "PASS" if image_check.returncode == 0 else "FAIL",
            "image",
            f"present: {image}" if image_check.returncode == 0 else f"missing: {image}",
        )
    )

    try:
        _ensure_exact_state_root(layout, environment)
        for directory in _private_state_directories(layout):
            ensure_private_directory(directory)
        checks.append(
            CheckResult(
                "PASS", "private-state", "required directories mode 0700"
            )
        )
    except (ValueError, OSError) as error:
        checks.append(
            CheckResult("FAIL", "private-state", _check_failure_detail(error))
        )

    for name, path in (
        ("codex-auth", layout.codex_auth_file),
        ("gh-hosts", layout.gh_dir / "hosts.yml"),
    ):
        try:
            ensure_private_file(path)
            checks.append(CheckResult("PASS", name, "present, mode 0600"))
        except (ValueError, OSError) as error:
            checks.append(CheckResult("FAIL", name, _check_failure_detail(error)))

    record: ProjectRecord | None = None
    try:
        record = _read_runtime_project(layout.project_file)
        checks.append(
            CheckResult(
                "PASS",
                "project-metadata",
                f"repository {record.repository.slug}",
            )
        )
    except (ValueError, OSError) as error:
        checks.append(
            CheckResult("FAIL", "project-metadata", _check_failure_detail(error))
        )

    if record is None:
        checks.append(
            CheckResult("FAIL", "workspace-origin", "project metadata unavailable")
        )
    else:
        try:
            validate_workspace(layout.workspace)
            validate_workspace_origin(
                layout.workspace,
                record.repository,
                git_remote_reader(layout.workspace),
            )
            checks.append(
                CheckResult("PASS", "workspace-origin", "exact HTTPS origin")
            )
        except (
            ValueError,
            subprocess.CalledProcessError,
            OSError,
        ) as error:
            checks.append(
                CheckResult(
                    "FAIL", "workspace-origin", _check_failure_detail(error)
                )
            )

    if record is None:
        checks.append(
            CheckResult("FAIL", "handover-project", "project metadata unavailable")
        )
    else:
        try:
            handover_root = _resolve_handover_root(
                record.handover_root, layout.project_id
            )
            checks.append(
                CheckResult(
                    "PASS",
                    "handover-project",
                    "real directory within configured root",
                )
            )
        except (ValueError, OSError) as error:
            checks.append(
                CheckResult(
                    "FAIL", "handover-project", _check_failure_detail(error)
                )
            )

    checks.append(
        CheckResult(
            "WARN",
            "network-policy",
            "outbound network is not domain-restricted in Phase 1",
        )
    )
    return checks


def main(
    argv: list[str] | None = None,
    environment: Mapping[str, str] | None = None,
    runner: Callable[[CommandSpec], subprocess.CompletedProcess] = run_command,
    git_remote_reader: Callable[[Path], str] = read_git_remote,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    identity_reader: Callable[[], tuple[int, int]] = read_process_identity,
    runtime_spec_builder: RuntimeSpecBuilder | None = None,
    runtime_spec_builders: Mapping[str, RuntimeSpecBuilder] | None = None,
    cachebuster_reader: Callable[[], str] = read_cachebuster,
) -> int:
    try:
        arguments = parser().parse_args(argv)
        _validate_image(arguments.image)
        if arguments.command == "build":
            validate_version(arguments.codex_version)
            validate_version(arguments.claude_version)
        elif arguments.command == "auth":
            validate_agent(arguments.agent)
        elif arguments.command == "run":
            validate_agent(arguments.agent)
        elif arguments.command == "doctor":
            validate_agent(arguments.agent, allow_all=True)
        elif arguments.command == "migrate":
            validate_agent(arguments.agent)
            for plugin in arguments.plugins:
                validate_plugin_identifier(plugin)
        repository_root = Path(__file__).resolve().parents[2]
        if arguments.command == "build":
            _podman_preflight(runner)
            runner(
                build_image_spec(
                    repository_root,
                    arguments.image,
                    arguments.codex_version,
                    arguments.claude_version,
                    cachebuster_reader(),
                )
            )
            for label, agent in (("Codex", "codex"), ("Claude", "claude")):
                version = _required_probe_run(
                    runner, cli_version_spec(arguments.image, agent)
                )
                print(f"{label} version: {(version.stdout or '').strip()}", file=stdout)
            return 0
        if arguments.command == "auth" and arguments.agent == "codex":
            layout = StateLayout.from_environment("auth", environment)
            _ensure_exact_state_root(layout, environment)
            _validate_existing_codex_auth(layout)
            _podman_preflight(runner, image_required=arguments.image)
            _prepare_codex_auth(layout, repository_root / "profiles/codex")
            runner(auth_codex_spec(layout, arguments.image))
            ensure_private_file(layout.codex_auth_file)
            status_spec = codex_login_status_spec(layout, arguments.image)
            _require_success(runner(status_spec), status_spec)
            return 0
        if arguments.command == "auth" and arguments.agent == "claude":
            layout = StateLayout.from_environment("auth", environment)
            _ensure_exact_state_root(layout, environment)
            _validate_existing_claude_auth(layout)
            _podman_preflight(runner, image_required=arguments.image)
            _prepare_claude_auth(layout)
            login_spec = auth_claude_spec(layout, arguments.image)
            _require_success(runner(login_spec), login_spec)
            ensure_private_directory(layout.claude_auth_dir)
            ensure_private_file(layout.claude_auth_file)
            status_spec = claude_login_status_spec(layout, arguments.image)
            _require_success(runner(status_spec), status_spec)
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
        if arguments.command == "run":
            layout, handover_project, uid, gid = _runtime_preflight(
                arguments.project,
                arguments.agent,
                environment,
                git_remote_reader,
                identity_reader,
            )
            _podman_preflight(runner, image_required=arguments.image)
            if arguments.agent == "claude":
                _prepare_claude_project_state(layout)
            builders = (
                {"codex": run_codex_spec, "claude": run_claude_spec}
                if runtime_spec_builders is None
                else runtime_spec_builders
            )
            if runtime_spec_builder is not None:
                builders = {**builders, "codex": runtime_spec_builder}
            spec = builders[arguments.agent](
                layout,
                handover_project,
                arguments.image,
                uid,
                gid,
            )
            print(
                f"Starting {arguments.agent.title()} for project: {layout.project_id}",
                file=stdout,
            )
            runner(spec)
            return 0
        if arguments.command == "doctor":
            checks = _doctor(
                arguments.project,
                arguments.image,
                environment,
                runner,
                git_remote_reader,
            )
            for check in checks:
                print(
                    f"{check.level}  {check.name}: {check.detail}",
                    file=stdout,
                )
            return 1 if any(check.level == "FAIL" for check in checks) else 0
        return 1
    except subprocess.CalledProcessError as error:
        print(f"error: command failed with exit code {error.returncode}", file=stderr)
        return error.returncode or 1
    except (ValueError, PermissionError, FileNotFoundError) as error:
        print(f"error: {error}", file=stderr)
        return 1
    except OSError:
        print("error: filesystem operation failed", file=stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
