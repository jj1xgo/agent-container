import argparse
from contextlib import ExitStack
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import tempfile
import termios
import threading
import time
from typing import Callable
from typing import Mapping
from typing import TextIO

from agent_container import __version__
from agent_container.egress_policy import add_egress_domain
from agent_container.egress_policy import disable_egress_policy
from agent_container.egress_policy import EgressPolicy
from agent_container.egress_policy import enable_egress_policy
from agent_container.egress_policy import load_egress_policy
from agent_container.egress_policy import remove_egress_domain
from agent_container.egress_policy import validate_domain
from agent_container.egress_broker_runtime import EgressBrokerRuntime
from agent_container.egress_broker_runtime import EgressBrokerRuntimeError
from agent_container.egress_broker_runtime import EgressRuntimeMount
from agent_container.claude_auth import discard_staged_token
from agent_container.claude_auth import install_claude_token
from agent_container.claude_auth import quarantine_legacy_claude_state
from agent_container.claude_auth import stage_claude_token
from agent_container.claude_auth import validate_legacy_quarantine_sources
from agent_container.migration import add_plugin_entries
from agent_container.migration import apply_claude_migration
from agent_container.migration import plan_claude_migration
from agent_container.migration import render_migration_plan
from agent_container.github_broker_runtime import GitHubBrokerRuntimeError
from agent_container.github_broker_runtime import UploadPackBrokerRuntime
from agent_container.github_broker_runtime import load_broker_policy
from agent_container.github_broker_runtime import upgrade_legacy_broker_policy
from agent_container.github_broker_runtime import write_broker_policy
from agent_container.handover_broker_runtime import HandoverBrokerRuntime
from agent_container.handover_broker_runtime import HandoverBrokerRuntimeError
from agent_container.github_app import GitHubAppMetadata
from agent_container.family_runtime_mount import FamilyRuntimeError
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_broker_policy import validate_repository_id
from agent_container.podman import CommandSpec
from agent_container.podman import auth_codex_spec
from agent_container.podman import build_image_spec
from agent_container.podman import build_project_image_spec
from agent_container.podman import clone_project_spec
from agent_container.podman import cli_version_spec
from agent_container.podman import codex_login_status_spec
from agent_container.podman import claude_setup_token_spec
from agent_container.podman import claude_token_status_spec
from agent_container.podman import claude_policy_status_spec
from agent_container.podman import handover_broker_client_status_spec
from agent_container.podman import egress_adapter_status_spec
from agent_container.podman import egress_runtime_stop_spec
from agent_container.podman import egress_runtime_kill_spec
from agent_container.podman import claude_superpowers_spec
from agent_container.podman import claude_superpowers_marketplace_spec
from agent_container.podman import codex_superpowers_install_spec
from agent_container.podman import codex_superpowers_marketplace_spec
from agent_container.podman import node_version_spec
from agent_container.podman import podman_image_exists_spec
from agent_container.podman import podman_architecture_spec
from agent_container.podman import podman_image_id_spec
from agent_container.podman import podman_project_images_spec
from agent_container.podman import podman_running_agent_containers_spec
from agent_container.podman import podman_rootless_spec
from agent_container.podman import podman_oci_runtime_spec
from agent_container.podman import podman_connections_spec
from agent_container.podman import podman_stats_spec
from agent_container.podman import podman_version_spec
from agent_container.podman import project_node_version_spec
from agent_container.podman import run_codex_spec
from agent_container.podman import run_claude_spec
from agent_container.podman import run_command
from agent_container.podman import run_command_supervised
from agent_container.podman import validate_claude_handover_project
from agent_container.profile import seed_codex_home
from agent_container.profile import update_codex_handover_profile
from agent_container.project_image import ProjectImageConfig
from agent_container.project_image import ProjectImageResolution
from agent_container.project_image import load_project_image_config
from agent_container.project_image import project_image_key
from agent_container.project_image import project_image_name
from agent_container.project_image import write_project_build_context
from agent_container.state import ProjectRecord
from agent_container.state import Repository
from agent_container.state import StateLayout
from agent_container.state import ensure_private_directory
from agent_container.state import ensure_private_file
from agent_container.state import validate_claude_oauth_token
from agent_container.state import validate_workspace
from agent_container.state import validate_workspace_origin
from agent_container.state import validate_agent
from agent_container.state import validate_plugin_identifier
from agent_container.state import validate_project_id
from agent_container.state import validate_version


DEFAULT_IMAGE = "localhost/agent-container:dev"
RuntimeSpecBuilder = Callable[..., CommandSpec]


def read_cachebuster() -> str:
    return str(time.time_ns())


@dataclass(frozen=True)
class CheckResult:
    level: str
    name: str
    detail: str


class _ClaudeTokenFilesystemError(Exception):
    pass


class _ClaudeRuntimeStateError(Exception):
    pass


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


def _positive_repository_id(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or value.startswith("0"):
        raise argparse.ArgumentTypeError(
            "GitHub repository ID must be a positive integer"
        )
    parsed = int(value)
    validate_repository_id(parsed)
    return parsed


def _positive_issue_number(value: str) -> int:
    if (
        len(value) > 10
        or not value.isascii()
        or not value.isdecimal()
        or value.startswith("0")
    ):
        raise argparse.ArgumentTypeError("issue number must be a positive integer")
    parsed = int(value)
    if parsed > 2_147_483_647:
        raise argparse.ArgumentTypeError("issue number must be a positive integer")
    return parsed


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="agentctl")
    command.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    command.add_argument("--image", default=DEFAULT_IMAGE)
    subcommands = command.add_subparsers(dest="command", required=True)
    build = subcommands.add_parser("build")
    build.add_argument("--node-version", default="latest")
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
    add.add_argument("--github-broker", action="store_true")
    add.add_argument("--github-repository-id", type=_positive_repository_id)
    add.add_argument("--default-branch", default="main")
    add.add_argument("--protected-branch", action="append", default=[])
    update_profile = project_subcommands.add_parser("update-profile")
    update_profile.add_argument("project")
    configure_egress = project_subcommands.add_parser("configure-egress")
    configure_egress.add_argument("project")
    egress_actions = configure_egress.add_mutually_exclusive_group(required=True)
    egress_actions.add_argument("--enable", action="store_true")
    egress_actions.add_argument("--add-domain")
    egress_actions.add_argument("--remove-domain")
    egress_actions.add_argument("--disable", action="store_true")
    superpowers = subcommands.add_parser("superpowers")
    superpowers_subcommands = superpowers.add_subparsers(
        dest="superpowers_command", required=True
    )
    superpowers_update = superpowers_subcommands.add_parser("update")
    superpowers_update.add_argument("project", nargs="?")
    superpowers_update.add_argument("--all-projects", action="store_true")
    run = subcommands.add_parser("run")
    run.add_argument("project")
    run.add_argument("--agent", choices=("codex", "claude"), default="codex")
    run.add_argument("--github-broker", action="store_true")
    stats = subcommands.add_parser("stats")
    stats.add_argument("project")
    doctor = subcommands.add_parser("doctor")
    doctor.add_argument("project")
    doctor.add_argument("--agent", choices=("codex", "claude", "all"), default="codex")
    doctor.add_argument("--github-broker", action="store_true")
    migrate = subcommands.add_parser("migrate")
    migrate.add_argument("agent", choices=("claude",))
    migrate.add_argument("project")
    migrate.add_argument("--from", dest="source", type=Path, required=True)
    migrate.add_argument("--plugin", dest="plugins", action="append", default=[])
    migrate.add_argument("--apply", action="store_true")
    family = subcommands.add_parser("family")
    family_subcommands = family.add_subparsers(dest="family_command", required=True)
    family_bind = family_subcommands.add_parser("bind")
    family_bind.add_argument("project")
    family_bind.add_argument("repository")
    family_list = family_subcommands.add_parser("list")
    family_list.add_argument("project")
    family_doctor = family_subcommands.add_parser("doctor")
    family_doctor.add_argument("project")
    family_issue = family_subcommands.add_parser("issue")
    family_issue_subcommands = family_issue.add_subparsers(
        dest="family_issue_command", required=True
    )
    family_pending = family_issue_subcommands.add_parser("pending")
    family_pending.add_argument("project")
    for operation in ("preview", "approve", "reject", "resolve-not-created"):
        action = family_issue_subcommands.add_parser(operation)
        action.add_argument("project")
        action.add_argument("request_id")
    family_resolve_created = family_issue_subcommands.add_parser("resolve-created")
    family_resolve_created.add_argument("project")
    family_resolve_created.add_argument("request_id")
    family_resolve_created.add_argument("issue_number", type=_positive_issue_number)
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
    try:
        ensure_private_directory(layout.root, create=True)
        ensure_private_directory(layout.root / "shared-auth", create=True)
        ensure_private_directory(layout.claude_auth_dir, create=True)
        if layout.claude_token_file.exists() or layout.claude_token_file.is_symlink():
            ensure_private_file(layout.claude_token_file)
    except (ValueError, OSError):
        raise _ClaudeTokenFilesystemError from None


def _validate_existing_claude_auth(layout: StateLayout) -> None:
    try:
        for directory in (
            layout.root,
            layout.root / "shared-auth",
            layout.claude_auth_dir,
        ):
            if directory.exists() or directory.is_symlink():
                ensure_private_directory(directory)
        if layout.claude_token_file.exists() or layout.claude_token_file.is_symlink():
            ensure_private_file(layout.claude_token_file)
    except (ValueError, OSError):
        raise _ClaudeTokenFilesystemError from None


def _snapshot_legacy_claude_sources(layout: StateLayout) -> tuple[Path, ...]:
    if not (layout.claude_auth_dir.exists() or layout.claude_auth_dir.is_symlink()):
        return ()
    try:
        return validate_legacy_quarantine_sources(layout)
    except (ValueError, OSError):
        raise _ClaudeTokenFilesystemError from None


def _open_controlling_terminal() -> int:
    return os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)


def _require_private_token_terminal(
    stdin: TextIO,
    stderr: TextIO,
    open_terminal: Callable[[], int] = _open_controlling_terminal,
) -> None:
    if not stdin.isatty() or not stderr.isatty():
        raise ValueError("Claude setup-token requires a private terminal")
    # The hidden prompt reads /dev/tty directly; fail here, before
    # `claude setup-token` displays the token, if there is no controlling terminal.
    try:
        descriptor = open_terminal()
    except OSError:
        raise ValueError(
            "Claude setup-token requires a controlling terminal (/dev/tty)"
        ) from None
    os.close(descriptor)


def read_hidden_token(
    prompt: str,
    *,
    open_terminal: Callable[[], int] = _open_controlling_terminal,
    settle_seconds: float = 1.0,
    limit: int = 8192,
) -> str:
    """Read a pasted token from the terminal without echo, including wrapped lines.

    getpass keeps only the first line, flushes the rest of the paste, and
    echoes anything it does not consume. A token that wrapped across two
    terminal lines is pasted with a line break, so this reader owns the
    terminal for the whole paste: echo and canonical mode are off, and after
    the first newline it keeps reading until the input has been quiet for
    ``settle_seconds``. Backspace and Ctrl-U edit the value as in canonical
    mode. Whatever is still queued when the reader returns is flushed so the
    shell never receives it. Everything read is returned as is; the caller
    joins the lines.
    """

    descriptor = open_terminal()
    try:
        try:
            saved = termios.tcgetattr(descriptor)
        except termios.error as error:
            raise OSError(str(error)) from None
        attributes = list(saved)
        attributes[3] &= ~(termios.ECHO | termios.ICANON)
        attributes[6] = list(attributes[6])
        attributes[6][termios.VMIN] = 1
        attributes[6][termios.VTIME] = 0
        pending = bytearray()
        try:
            termios.tcsetattr(descriptor, termios.TCSAFLUSH, attributes)
            os.write(descriptor, prompt.encode("utf-8"))
            newline_seen = False
            finished = False
            while not finished and len(pending) < limit:
                timeout = settle_seconds if newline_seen else None
                ready, _, _ = select.select([descriptor], [], [], timeout)
                if not ready:
                    break
                chunk = os.read(descriptor, 4096)
                if not chunk:
                    break
                for byte in chunk:
                    if byte == 0x04 or len(pending) >= limit:
                        finished = True
                        break
                    if byte in (0x7F, 0x08):
                        if pending:
                            del pending[-1]
                        continue
                    if byte == 0x15:
                        pending.clear()
                        continue
                    pending.append(byte)
                    if byte in (0x0A, 0x0D):
                        newline_seen = True
            if finished and not pending.strip():
                raise EOFError("hidden token input ended")
        finally:
            os.write(descriptor, b"\n")
            # TCSAFLUSH discards anything still queued (an over-long paste or a
            # tail that arrived after the settle window) instead of handing it
            # to the shell.
            try:
                termios.tcsetattr(descriptor, termios.TCSAFLUSH, saved)
            except termios.error as error:
                raise OSError(str(error)) from None
    finally:
        os.close(descriptor)
    return pending.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def _normalize_pasted_token(raw: str) -> tuple[str, int]:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    return "".join(lines), len(lines)


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


def _prepare_project_directories(
    layout: StateLayout, include_gh: bool = True
) -> None:
    ensure_private_directory(layout.root, create=True)
    if include_gh:
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


def _install_superpowers(
    layout: StateLayout,
    image: str,
    runner: Callable[[CommandSpec], subprocess.CompletedProcess],
    *,
    update: bool = False,
) -> None:
    ensure_private_directory(layout.codex_home)
    ensure_private_directory(layout.claude_config, create=True)
    codex_marketplace = layout.codex_home / ".tmp/marketplaces/superpowers-dev"
    claude_marketplace = (
        layout.claude_config / "plugins/marketplaces/claude-plugins-official"
    )
    claude_plugin = (
        layout.claude_config / "plugins/cache/claude-plugins-official/superpowers"
    )
    for path in (codex_marketplace, claude_marketplace, claude_plugin):
        if path.is_symlink():
            raise ValueError(f"managed plugin path must not be a symlink: {path}")
    marketplace = codex_superpowers_marketplace_spec(
        layout, image, update=update and codex_marketplace.is_dir()
    )
    _require_success(runner(marketplace), marketplace)
    codex_install = codex_superpowers_install_spec(layout, image)
    _require_success(runner(codex_install), codex_install)
    claude_marketplace_command = claude_superpowers_marketplace_spec(
        layout, image, update=update and claude_marketplace.is_dir()
    )
    _require_success(
        runner(claude_marketplace_command), claude_marketplace_command
    )
    claude_install = claude_superpowers_spec(
        layout, image, update=update and claude_plugin.is_dir()
    )
    _require_success(runner(claude_install), claude_install)


def _add_project(
    repository_value: str,
    project_value: str | None,
    handover_root: Path,
    image: str,
    environment: Mapping[str, str] | None,
    runner: Callable[[CommandSpec], subprocess.CompletedProcess],
    git_remote_reader: Callable[[Path], str],
    profile_root: Path,
    github_broker: bool = False,
    default_branch: str = "main",
    protected_branches: tuple[str, ...] = (),
    github_repository_id: int | None = None,
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
    policy: BrokerPolicy | None = None
    existing_policy: BrokerPolicy | None = None
    policy_was_present = False
    upgrade_interrupted_legacy = False
    completed_legacy_project = False
    record = ProjectRecord(repository, resolved_handover_root)
    if github_broker:
        protected = protected_branches or (default_branch,)
        if github_repository_id is None:
            completed_state = (
                workspace_exists
                and (layout.project_file.exists() or layout.project_file.is_symlink())
                and (
                    layout.github_broker_policy_file.exists()
                    or layout.github_broker_policy_file.is_symlink()
                )
            )
            if not completed_state:
                raise ValueError(
                    "GitHub repository ID is required for new broker projects"
                )
            if _read_runtime_project(layout.project_file) != record:
                raise ValueError("project metadata does not match project")
            policy = load_broker_policy(
                layout.github_broker_policy_file, record, layout.project_id
            )
            expected_legacy = BrokerPolicy.create(
                project_id=layout.project_id,
                repository=repository.slug,
                default_branch=default_branch,
                protected_branches=protected,
            )
            if policy.repository_id is not None or policy != expected_legacy:
                raise ValueError("GitHub broker policy does not match project")
            completed_legacy_project = True
        else:
            policy = BrokerPolicy.create(
                project_id=layout.project_id,
                repository=repository.slug,
                repository_id=github_repository_id,
                default_branch=default_branch,
                protected_branches=protected,
                require_repository_id=True,
            )
            policy_was_present = (
                layout.github_broker_policy_file.exists()
                or layout.github_broker_policy_file.is_symlink()
            )
            if policy_was_present:
                existing_policy = load_broker_policy(
                    layout.github_broker_policy_file, record, layout.project_id
                )
                expected_legacy = BrokerPolicy.create(
                    project_id=layout.project_id,
                    repository=repository.slug,
                    default_branch=default_branch,
                    protected_branches=protected,
                )
                if existing_policy == policy:
                    pass
                elif existing_policy == expected_legacy:
                    if (
                        layout.project_file.exists()
                        or layout.project_file.is_symlink()
                        or workspace_exists
                    ):
                        raise ValueError(
                            "interrupted GitHub broker registration state does not match"
                        )
                    upgrade_interrupted_legacy = True
                else:
                    raise ValueError("GitHub broker policy does not match project")
        GitHubAppMetadata.load(
            layout.github_broker_root / "app.json",
            layout.github_broker_root / "private-key.pem",
        )
    _podman_preflight(runner, image_required=image)
    if upgrade_interrupted_legacy:
        assert existing_policy is not None
        assert policy is not None
        if (
            layout.project_file.exists()
            or layout.project_file.is_symlink()
            or layout.workspace.exists()
            or layout.workspace.is_symlink()
        ):
            raise ValueError(
                "interrupted GitHub broker registration state does not match"
            )
        upgrade_legacy_broker_policy(
            layout.github_broker_policy_file, existing_policy, policy
        )
    _prepare_project_directories(layout, include_gh=not github_broker)
    if github_broker:
        assert policy is not None
        if upgrade_interrupted_legacy:
            existing = load_broker_policy(
                layout.github_broker_policy_file, record, layout.project_id
            )
            if existing != policy:
                raise ValueError("GitHub broker policy does not match project")
        elif policy_was_present or completed_legacy_project:
            existing = load_broker_policy(
                layout.github_broker_policy_file, record, layout.project_id
            )
            if existing != policy:
                raise ValueError("GitHub broker policy does not match project")
        else:
            write_broker_policy(layout.github_broker_policy_file, policy)
    else:
        ensure_private_file(layout.gh_dir / "hosts.yml")
    if not workspace_exists:
        if github_broker:
            with UploadPackBrokerRuntime.create(layout, record) as broker:
                runner(clone_project_spec(layout, repository, image, broker))
        else:
            runner(clone_project_spec(layout, repository, image))
        validate_workspace(layout.workspace)
        validate_workspace_origin(
            layout.workspace, repository, git_remote_reader(layout.workspace)
        )
    _seed_project_codex_home(layout, profile_root)
    _install_superpowers(layout, image, runner)
    if not completed_legacy_project:
        record.write(layout.project_file)


def _runtime_preflight(
    project_id: str,
    agent: str,
    environment: Mapping[str, str] | None,
    git_remote_reader: Callable[[Path], str],
    identity_reader: Callable[[], tuple[int, int]],
    github_broker: bool = False,
) -> tuple[StateLayout, ProjectRecord, Path, int, int, EgressPolicy | None]:
    layout = StateLayout.from_environment(project_id, environment)
    _ensure_exact_state_root(layout, environment)
    for directory in _common_runtime_state_directories(
        layout, include_gh=not github_broker
    ):
        ensure_private_directory(directory)
    _validate_runtime_agent_state(layout, agent)
    if not github_broker:
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
    if agent == "claude":
        validate_claude_handover_project(layout, handover_project)
    egress_policy = _load_optional_egress_policy(layout.egress_policy_file)
    uid, gid = _validated_process_identity(identity_reader)
    return layout, record, handover_project, uid, gid, egress_policy


def _load_optional_egress_policy(path: Path) -> EgressPolicy | None:
    if not path.exists() and not path.is_symlink():
        return None
    return load_egress_policy(path)


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


def _common_runtime_state_directories(
    layout: StateLayout, include_gh: bool = True
) -> tuple[Path, ...]:
    directories = [
        layout.root,
        layout.root / "shared-auth",
        layout.project_dir.parent,
        layout.project_dir,
        layout.cache,
        layout.workspace.parent,
    ]
    if include_gh:
        directories.insert(2, layout.gh_dir)
    return tuple(directories)


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
        return layout.claude_token_file
    raise ValueError("agent must be codex or claude")


def _validate_claude_token_file(path: Path) -> None:
    ensure_private_file(path)
    try:
        token = path.read_text(encoding="ascii")
    except UnicodeError:
        raise ValueError("Claude OAuth token has invalid format") from None
    validate_claude_oauth_token(token)


def _validate_claude_project_config(layout: StateLayout) -> None:
    credentials = layout.claude_config / ".credentials.json"
    if credentials.exists() or credentials.is_symlink():
        raise ValueError("unsupported legacy Claude project credentials")


def _validate_runtime_agent_state(layout: StateLayout, agent: str) -> None:
    if agent == "claude":
        try:
            for directory in _runtime_agent_directories(layout, agent):
                ensure_private_directory(directory)
            _validate_claude_token_file(layout.claude_token_file)
        except (ValueError, OSError):
            raise _ClaudeRuntimeStateError from None
        _validate_claude_project_config(layout)
        return
    for directory in _runtime_agent_directories(layout, agent):
        ensure_private_directory(directory)
    ensure_private_file(_runtime_agent_auth_file(layout, agent))


def _private_state_directories(
    layout: StateLayout, agents: tuple[str, ...], include_gh: bool = True
) -> tuple[Path, ...]:
    directories = list(_common_runtime_state_directories(layout, include_gh))
    for agent in agents:
        directories.extend(_runtime_agent_directories(layout, agent))
    return tuple(directories)


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


def _suppressed_run(
    runner: Callable[[CommandSpec], subprocess.CompletedProcess],
    spec: CommandSpec,
) -> subprocess.CompletedProcess[str]:
    if runner is run_command:
        completed = run_command(spec, check=False, capture_output=True)
    else:
        completed = runner(spec)
    return subprocess.CompletedProcess(spec.argv, completed.returncode)


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


def _family_podman_preflight(
    runner: Callable[[CommandSpec], subprocess.CompletedProcess],
    environment: Mapping[str, str] | None = None,
) -> None:
    effective_environment = os.environ if environment is None else environment
    if any(
        effective_environment.get(name)
        for name in ("CONTAINER_HOST", "CONTAINER_CONNECTION")
    ):
        raise ValueError("local Podman with crun is required for family intake")
    version_result = _required_probe_run(runner, podman_version_spec())
    version_match = re.fullmatch(
        r"podman version ([0-9]+)\.([0-9]+)(?:\.[0-9]+)?",
        (version_result.stdout or "").strip(),
    )
    if version_match is None or (
        int(version_match.group(1)), int(version_match.group(2))
    ) < (5, 8):
        raise ValueError("Podman 5.8 or newer is required for family intake")
    rootless = _required_probe_run(runner, podman_rootless_spec())
    if (rootless.stdout or "").strip().lower() != "true":
        raise ValueError("rootless Podman is required for family intake")
    runtime = _required_probe_run(runner, podman_oci_runtime_spec())
    if (runtime.stdout or "").strip() != "crun":
        raise ValueError("local Podman with crun is required for family intake")
    connections = _required_probe_run(runner, podman_connections_spec())
    try:
        decoded = json.loads(connections.stdout or "")
    except (json.JSONDecodeError, TypeError):
        raise ValueError("local Podman with crun is required for family intake") from None
    if type(decoded) is not list:
        raise ValueError("local Podman with crun is required for family intake")
    for entry in decoded:
        if type(entry) is not dict:
            raise ValueError("local Podman with crun is required for family intake")
        default = entry.get("Default", entry.get("default", False))
        if type(default) is not bool or default:
            raise ValueError("local Podman with crun is required for family intake")


def _family_podman_doctor_checks(
    runner: Callable[[CommandSpec], subprocess.CompletedProcess],
    version: subprocess.CompletedProcess,
    environment: Mapping[str, str] | None,
) -> list[CheckResult]:
    version_text = (version.stdout or "").strip()
    match = re.fullmatch(
        r"podman version ([0-9]+)\.([0-9]+)(?:\.[0-9]+)?", version_text
    )
    version_ok = (
        version.returncode == 0
        and match is not None
        and (int(match.group(1)), int(match.group(2))) >= (5, 8)
    )
    runtime = _doctor_run(runner, podman_oci_runtime_spec())
    runtime_ok = runtime.returncode == 0 and (runtime.stdout or "").strip() == "crun"
    connections = _doctor_run(runner, podman_connections_spec())
    effective_environment = os.environ if environment is None else environment
    environment_local = not any(
        effective_environment.get(name)
        for name in ("CONTAINER_HOST", "CONTAINER_CONNECTION")
    )
    local_ok = False
    if connections.returncode == 0:
        try:
            decoded = json.loads(connections.stdout or "")
            local_ok = environment_local and type(decoded) is list and all(
                type(entry) is dict
                and type(entry.get("Default", entry.get("default", False))) is bool
                and not entry.get("Default", entry.get("default", False))
                for entry in decoded
            )
        except (json.JSONDecodeError, TypeError):
            local_ok = False
    return [
        CheckResult(
            "PASS" if version_ok else "FAIL",
            "family-podman-version",
            ">= 5.8 required",
        ),
        CheckResult(
            "PASS" if runtime_ok else "FAIL",
            "family-podman-runtime",
            "crun required",
        ),
        CheckResult(
            "PASS" if local_ok else "FAIL",
            "family-podman-local",
            "local service required",
        ),
    ]


def _resolve_project_image(
    layout: StateLayout,
    base_image: str,
    runner: Callable[[CommandSpec], subprocess.CompletedProcess],
    build_missing: bool,
    stdout: TextIO,
    config: ProjectImageConfig | None = None,
    base_image_id: str | None = None,
) -> ProjectImageResolution:
    if config is None:
        config = load_project_image_config(layout.workspace)
    if config.is_empty:
        return ProjectImageResolution(base_image, "unconfigured", None)

    if base_image_id is None:
        base_id_result = _required_probe_run(runner, podman_image_id_spec(base_image))
        base_image_id = (base_id_result.stdout or "").strip()
    architecture_result = _required_probe_run(runner, podman_architecture_spec())
    architecture = (architecture_result.stdout or "").strip()
    key = project_image_key(base_image_id, config, architecture)
    image = project_image_name(layout.project_id, key)

    present = _doctor_run(runner, podman_image_exists_spec(image))
    if present.returncode == 0:
        return ProjectImageResolution(image, "current", key)

    if not build_missing:
        prior = _doctor_run(runner, podman_project_images_spec(layout.project_id))
        has_prior = prior.returncode == 0 and bool((prior.stdout or "").strip())
        state = "stale" if has_prior else "missing"
        return ProjectImageResolution(image, state, key)

    print("project image missing; building", file=stdout)
    with tempfile.TemporaryDirectory(prefix="agent-container-project-") as temporary:
        context = Path(temporary)
        containerfile = write_project_build_context(context, base_image, config)
        build_spec = build_project_image_spec(
            context, containerfile, base_image, image
        )
        _require_success(runner(build_spec), build_spec)
    return ProjectImageResolution(image, "current", key)


def _check_failure_detail(error: Exception) -> str:
    if isinstance(error, OSError):
        return "filesystem operation failed"
    if isinstance(error, ValueError):
        return "state validation failed"
    if isinstance(error, subprocess.CalledProcessError):
        return "command failed"
    return "check failed"


def _reported_node_version(completed: subprocess.CompletedProcess) -> str | None:
    if completed.returncode != 0:
        return None
    value = (completed.stdout or "").strip()
    numeric = value[1:] if value.startswith("v") else ""
    if len(numeric.split(".")) != 3 or not all(
        part.isdigit() for part in numeric.split(".")
    ):
        return None
    return value


def _doctor(
    project_id: str,
    agent: str,
    image: str,
    environment: Mapping[str, str] | None,
    runner: Callable[[CommandSpec], subprocess.CompletedProcess],
    git_remote_reader: Callable[[Path], str],
    github_broker: bool = False,
) -> list[CheckResult]:
    layout = StateLayout.from_environment(project_id, environment)
    checks: list[CheckResult] = []
    agents = ("codex", "claude") if agent == "all" else (agent,)

    version = _doctor_run(runner, podman_version_spec())
    checks.append(
        CheckResult(
            "PASS" if version.returncode == 0 else "FAIL",
            "podman-version",
            "available" if version.returncode == 0 else "unavailable",
        )
    )

    from agent_container.family_state import FamilyStateLayout

    family_layout = FamilyStateLayout(layout.root, layout.project_id)
    try:
        os.lstat(family_layout.family_binding_file)
    except FileNotFoundError:
        pass
    else:
        checks.extend(_family_podman_doctor_checks(runner, version, environment))

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

    runtime_image: str | None = None
    project_config: ProjectImageConfig | None = None
    base_image_id: str | None = None
    if image_check.returncode != 0:
        checks.append(CheckResult("FAIL", "base-image-id", "image unavailable"))
        checks.append(
            CheckResult("FAIL", "project-image", "base image unavailable")
        )
    else:
        identity = _doctor_run(runner, podman_image_id_spec(image))
        candidate_id = (identity.stdout or "").strip()
        try:
            project_image_key(
                candidate_id,
                ProjectImageConfig((), None),
                "doctor",
            )
            identity_ok = identity.returncode == 0
        except ValueError:
            identity_ok = False
        if identity_ok:
            base_image_id = candidate_id
        checks.append(
            CheckResult(
                "PASS" if identity_ok else "FAIL",
                "base-image-id",
                candidate_id if identity_ok else "unavailable",
            )
        )
        try:
            project_config = load_project_image_config(layout.workspace)
            if base_image_id is None:
                raise ValueError("base image identity unavailable")
            resolution = _resolve_project_image(
                layout,
                image,
                runner,
                build_missing=False,
                stdout=sys.stdout,
                config=project_config,
                base_image_id=base_image_id,
            )
            usable = resolution.state in ("unconfigured", "current")
            checks.append(
                CheckResult(
                    "PASS" if usable else "FAIL",
                    "project-image",
                    resolution.state,
                )
            )
            if usable:
                runtime_image = resolution.image
        except (ValueError, OSError, subprocess.CalledProcessError) as error:
            checks.append(
                CheckResult(
                    "FAIL", "project-image", _check_failure_detail(error)
                )
            )

    if runtime_image is None:
        agent_node_version = None
    else:
        agent_node_version = _reported_node_version(
            _doctor_run(runner, node_version_spec(runtime_image))
        )
    checks.append(
        CheckResult(
            "PASS" if agent_node_version is not None else "FAIL",
            "agent-node",
            agent_node_version or "image unavailable",
        )
    )

    if project_config is not None and project_config.node_version is None:
        checks.append(CheckResult("PASS", "project-node", "unconfigured"))
    elif runtime_image is None or project_config is None:
        checks.append(CheckResult("FAIL", "project-node", "image unavailable"))
    else:
        expected_project_node = f"v{project_config.node_version}"
        observed_project_node = _reported_node_version(
            _doctor_run(runner, project_node_version_spec(runtime_image))
        )
        project_node_ok = observed_project_node == expected_project_node
        checks.append(
            CheckResult(
                "PASS" if project_node_ok else "FAIL",
                "project-node",
                expected_project_node if project_node_ok else "unexpected version",
            )
        )

    if "claude" in agents:
        if runtime_image is None:
            policy_ok = False
            policy_detail = "image unavailable"
        else:
            policy_status = _doctor_run(
                runner, claude_policy_status_spec(runtime_image)
            )
            policy_ok = policy_status.returncode == 0
            policy_detail = "valid" if policy_ok else "invalid"
        checks.append(
            CheckResult(
                "PASS" if policy_ok else "FAIL",
                "claude-managed-policy",
                policy_detail,
            )
        )

        if runtime_image is None:
            handover_client_ok = False
            handover_client_detail = "image unavailable"
        else:
            handover_client_status = _doctor_run(
                runner,
                handover_broker_client_status_spec(runtime_image),
            )
            handover_client_ok = handover_client_status.returncode == 0
            handover_client_detail = (
                "available" if handover_client_ok else "unavailable"
            )
        checks.append(
            CheckResult(
                "PASS" if handover_client_ok else "FAIL",
                "claude-handover-client",
                handover_client_detail,
            )
        )

    for selected_agent in agents:
        if runtime_image is not None:
            cli_version = _doctor_run(
                runner, cli_version_spec(runtime_image, selected_agent)
            )
            version_ok = cli_version.returncode == 0
            version_detail = "available" if version_ok else "unavailable"
        else:
            version_ok = False
            version_detail = "image unavailable"
        checks.append(
            CheckResult(
                "PASS" if version_ok else "FAIL",
                f"{selected_agent}-version",
                version_detail,
            )
        )

    try:
        _ensure_exact_state_root(layout, environment)
        for directory in _private_state_directories(
            layout, agents, include_gh=not github_broker
        ):
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

    claude_token_ok = False
    for selected_agent in agents:
        name = f"{selected_agent}-auth"
        path = _runtime_agent_auth_file(layout, selected_agent)
        try:
            if selected_agent == "claude":
                _validate_claude_token_file(path)
                claude_token_ok = True
            else:
                ensure_private_file(path)
            checks.append(CheckResult("PASS", name, "present, mode 0600"))
        except (ValueError, OSError) as error:
            checks.append(CheckResult("FAIL", name, _check_failure_detail(error)))

    if "claude" in agents:
        if not claude_token_ok:
            checks.append(
                CheckResult(
                    "FAIL",
                    "claude-auth-status",
                    "not run: token invalid",
                )
            )
        elif image_check.returncode != 0:
            checks.append(
                CheckResult(
                    "FAIL",
                    "claude-auth-status",
                    "not run: image unavailable",
                )
            )
        else:
            auth_status = _doctor_run(
                runner,
                claude_token_status_spec(layout.claude_token_file, image),
            )
            authenticated = auth_status.returncode == 0
            checks.append(
                CheckResult(
                    "PASS" if authenticated else "FAIL",
                    "claude-auth-status",
                    "authenticated" if authenticated else "command failed",
                )
            )

        try:
            ensure_private_directory(layout.claude_config)
            checks.append(
                CheckResult("PASS", "claude-config", "present, mode 0700")
            )
        except (ValueError, OSError) as error:
            checks.append(
                CheckResult("FAIL", "claude-config", _check_failure_detail(error))
            )

        try:
            _validate_claude_project_config(layout)
            checks.append(
                CheckResult(
                    "PASS", "claude-project-credentials", "absent"
                )
            )
        except (ValueError, OSError) as error:
            checks.append(
                CheckResult(
                    "FAIL",
                    "claude-project-credentials",
                    _check_failure_detail(error),
                )
            )

    if not github_broker:
        try:
            ensure_private_file(layout.gh_dir / "hosts.yml")
            checks.append(CheckResult("PASS", "gh-hosts", "present, mode 0600"))
        except (ValueError, OSError) as error:
            checks.append(CheckResult("FAIL", "gh-hosts", _check_failure_detail(error)))

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

    if github_broker:
        if record is None:
            checks.append(
                CheckResult("FAIL", "github-broker", "project metadata unavailable")
            )
        else:
            try:
                GitHubAppMetadata.load(
                    layout.github_broker_root / "app.json",
                    layout.github_broker_root / "private-key.pem",
                )
                policy = load_broker_policy(
                    layout.github_broker_policy_file, record, layout.project_id
                )
                binding = (
                    "project repository binding"
                    if policy.repository_id is not None
                    else "legacy global repository binding"
                )
                checks.append(
                    CheckResult(
                        "PASS",
                        "github-broker",
                        f"local App and {binding} valid",
                    )
                )
            except (ValueError, OSError) as error:
                checks.append(
                    CheckResult(
                        "FAIL", "github-broker", _check_failure_detail(error)
                    )
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
            if "claude" in agents:
                validate_claude_handover_project(
                    layout,
                    handover_root / layout.project_id,
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

    try:
        egress_policy = _load_optional_egress_policy(layout.egress_policy_file)
        if egress_policy is None:
            checks.append(
                CheckResult(
                    "WARN",
                    "network-policy",
                    "outbound network is not domain-restricted",
                )
            )
        elif runtime_image is None:
            checks.append(
                CheckResult(
                    "FAIL", "network-policy", "managed egress adapter unavailable"
                )
            )
        else:
            adapter = _doctor_run(
                runner, egress_adapter_status_spec(runtime_image)
            )
            checks.append(
                CheckResult(
                    "PASS" if adapter.returncode == 0 else "FAIL",
                    "network-policy",
                    (
                        "outbound HTTPS uses the project domain allowlist"
                        if adapter.returncode == 0
                        else "managed egress adapter self-check failed"
                    ),
                )
            )
    except (ValueError, OSError) as error:
        checks.append(
            CheckResult("FAIL", "network-policy", _check_failure_detail(error))
        )
    return checks


def _run_with_egress_supervision(
    runner: Callable[[CommandSpec], subprocess.CompletedProcess],
    spec: CommandSpec,
    gateway: EgressBrokerRuntime,
    mount: EgressRuntimeMount,
) -> subprocess.CompletedProcess:
    results: list[subprocess.CompletedProcess] = []
    errors: list[BaseException] = []

    def execute() -> None:
        try:
            results.append(runner(spec))
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=execute, name="podman-egress-runtime", daemon=True)
    worker.start()
    while worker.is_alive():
        if gateway.wait_failed(0.1):
            stop_deadline = time.monotonic() + 5
            while worker.is_alive() and time.monotonic() < stop_deadline:
                try:
                    runner(egress_runtime_stop_spec(mount))
                except (OSError, subprocess.CalledProcessError):
                    pass
                worker.join(0.1)
            while worker.is_alive():
                try:
                    runner(egress_runtime_kill_spec(mount))
                except (OSError, subprocess.CalledProcessError):
                    pass
                worker.join(0.1)
            raise EgressBrokerRuntimeError("egress gateway failed")
    worker.join()
    if errors:
        raise errors[0]
    if len(results) != 1:
        raise EgressBrokerRuntimeError("egress runtime supervision failed")
    return results[0]


def _approve_family_issue(
    layout,
    request_id: str,
    *,
    provider_factory: Callable,
    inventory,
    creator,
    stdin: TextIO,
    stdout: TextIO,
    clock: Callable[[], int],
) -> int:
    """Run one host-approved Issue creation with explicit operator streams."""

    from agent_container.family_cli import approve_family_issue

    return approve_family_issue(
        layout,
        request_id,
        provider_factory=provider_factory,
        inventory=inventory,
        creator=creator,
        stdin=stdin,
        stdout=stdout,
        clock=clock,
    )


def main(
    argv: list[str] | None = None,
    environment: Mapping[str, str] | None = None,
    runner: Callable[[CommandSpec], subprocess.CompletedProcess] = run_command,
    git_remote_reader: Callable[[Path], str] = read_git_remote,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    stdin: TextIO = sys.stdin,
    identity_reader: Callable[[], tuple[int, int]] = read_process_identity,
    runtime_spec_builder: RuntimeSpecBuilder | None = None,
    runtime_spec_builders: Mapping[str, RuntimeSpecBuilder] | None = None,
    cachebuster_reader: Callable[[], str] = read_cachebuster,
    token_reader: Callable[[str], str] | None = None,
    family_token_provider_factory: Callable | None = None,
    family_inventory: object | None = None,
    family_creator: object | None = None,
    family_clock: Callable[[], int] | None = None,
    family_runtime_factory: Callable | None = None,
    runtime_supervisor: Callable | None = None,
) -> int:
    try:
        arguments = parser().parse_args(argv)
        _validate_image(arguments.image)
        if arguments.command == "build":
            validate_version(arguments.node_version)
            validate_version(arguments.codex_version)
            validate_version(arguments.claude_version)
        elif arguments.command == "auth":
            validate_agent(arguments.agent)
        elif arguments.command == "run":
            validate_agent(arguments.agent)
        elif arguments.command == "stats":
            validate_project_id(arguments.project)
        elif arguments.command == "project" and arguments.project_command == "add":
            broker_options = (
                arguments.github_repository_id is not None
                or arguments.default_branch != "main"
                or bool(arguments.protected_branch)
            )
            if broker_options and not arguments.github_broker:
                raise ValueError("GitHub broker options require --github-broker")
        elif arguments.command == "project" and arguments.project_command == "update-profile":
            validate_project_id(arguments.project)
        elif arguments.command == "project" and arguments.project_command == "configure-egress":
            validate_project_id(arguments.project)
            if arguments.add_domain is not None:
                validate_domain(arguments.add_domain)
            if arguments.remove_domain is not None:
                validate_domain(arguments.remove_domain)
        elif arguments.command == "superpowers":
            if bool(arguments.project) == bool(arguments.all_projects):
                raise ValueError("choose one project or --all-projects")
            if arguments.project:
                validate_project_id(arguments.project)
        elif arguments.command == "doctor":
            validate_agent(arguments.agent, allow_all=True)
        elif arguments.command == "migrate":
            validate_agent(arguments.agent)
            validate_project_id(arguments.project)
            for plugin in arguments.plugins:
                validate_plugin_identifier(plugin)
        elif arguments.command == "family":
            validate_project_id(arguments.project)
        repository_root = Path(__file__).resolve().parents[2]
        if arguments.command == "family":
            try:
                from agent_container.family_cli import dispatch_family
                from agent_container.family_state import FamilyStateLayout
                state_layout = StateLayout.from_environment(
                    arguments.project, environment
                )
                _ensure_exact_state_root(state_layout, environment)
                for directory in (
                    state_layout.root,
                    state_layout.project_dir.parent,
                    state_layout.project_dir,
                ):
                    ensure_private_directory(directory)
                _read_runtime_project(state_layout.project_file)
                family_layout = FamilyStateLayout(
                    state_layout.root, state_layout.project_id
                )
                family_clock_source = (
                    (lambda: int(time.time()))
                    if family_clock is None
                    else family_clock
                )
                return dispatch_family(
                    arguments,
                    family_layout,
                    stdin=stdin,
                    stdout=stdout,
                    provider_factory=family_token_provider_factory,
                    inventory=family_inventory,
                    creator=family_creator,
                    clock=family_clock_source,
                    approval_handler=_approve_family_issue,
                )
            except Exception:
                raise ValueError("family command failed") from None
        if arguments.command == "build":
            _podman_preflight(runner)
            runner(
                build_image_spec(
                    repository_root,
                    arguments.image,
                    arguments.node_version,
                    arguments.codex_version,
                    arguments.claude_version,
                    cachebuster_reader(),
                    __version__,
                )
            )
            probes = (
                ("Node", node_version_spec(arguments.image)),
                ("Codex", cli_version_spec(arguments.image, "codex")),
                ("Claude", cli_version_spec(arguments.image, "claude")),
            )
            for label, probe in probes:
                version = _required_probe_run(runner, probe)
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
            legacy_sources = _snapshot_legacy_claude_sources(layout)
            _podman_preflight(runner, image_required=arguments.image)
            if token_reader is None:
                _require_private_token_terminal(stdin, stderr)
            _prepare_claude_auth(layout)
            setup_spec = claude_setup_token_spec(arguments.image)
            _require_success(runner(setup_spec), setup_spec)
            reader = read_hidden_token if token_reader is None else token_reader
            token = ""
            raw = ""
            try:
                try:
                    raw = reader("Paste the sk-ant-oat01- token printed by claude setup-token (input hidden): ")
                except (EOFError, KeyboardInterrupt):
                    print("error: Claude token input cancelled", file=stderr)
                    return 1
                token, line_count = _normalize_pasted_token(raw)
                if line_count > 1:
                    print(
                        f"note: joined {line_count} input lines from the hidden prompt",
                        file=stderr,
                    )
                try:
                    validate_claude_oauth_token(token)
                except ValueError as error:
                    if line_count > 1:
                        raise ValueError(
                            f"{error}; the input contained a line break, so the "
                            "token probably wrapped in the terminal: paste it "
                            "again in a wider terminal"
                        ) from None
                    raise
                try:
                    staged = stage_claude_token(layout.claude_auth_dir, token)
                except (ValueError, OSError):
                    raise _ClaudeTokenFilesystemError from None
            finally:
                token = ""
                raw = ""
            try:
                status_spec = claude_token_status_spec(staged, arguments.image)
                _require_success(_suppressed_run(runner, status_spec), status_spec)
                try:
                    install_claude_token(staged, layout.claude_token_file)
                    quarantine_legacy_claude_state(layout, legacy_sources)
                except (ValueError, OSError):
                    raise _ClaudeTokenFilesystemError from None
            finally:
                try:
                    discard_staged_token(staged)
                except (ValueError, OSError):
                    raise _ClaudeTokenFilesystemError from None
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
                arguments.github_broker,
                arguments.default_branch,
                tuple(arguments.protected_branch),
                arguments.github_repository_id,
            )
            return 0
        if arguments.command == "project" and arguments.project_command == "update-profile":
            layout = StateLayout.from_environment(arguments.project, environment)
            _ensure_exact_state_root(layout, environment)
            ensure_private_directory(layout.root)
            ensure_private_directory(layout.project_dir.parent)
            ensure_private_directory(layout.project_dir)
            ensure_private_directory(layout.codex_home)
            _read_runtime_project(layout.project_file)
            update_codex_handover_profile(
                repository_root / "profiles/codex", layout.codex_home
            )
            print(
                f"Updated managed handover profile for project: {layout.project_id}",
                file=stdout,
            )
            return 0
        if arguments.command == "project" and arguments.project_command == "configure-egress":
            layout = StateLayout.from_environment(arguments.project, environment)
            _ensure_exact_state_root(layout, environment)
            for directory in (layout.root, layout.project_dir.parent, layout.project_dir):
                ensure_private_directory(directory)
            _read_runtime_project(layout.project_file)
            if arguments.enable:
                enable_egress_policy(layout.egress_policy_file)
                detail = "enabled"
            elif arguments.add_domain is not None:
                add_egress_domain(layout.egress_policy_file, arguments.add_domain)
                detail = "updated"
            elif arguments.remove_domain is not None:
                remove_egress_domain(layout.egress_policy_file, arguments.remove_domain)
                detail = "updated"
            else:
                disable_egress_policy(layout.egress_policy_file)
                detail = "disabled; the next runtime has unrestricted outbound networking"
            print(
                f"Project egress policy {detail}: {layout.project_id}",
                file=stdout,
            )
            return 0
        if arguments.command == "superpowers" and arguments.superpowers_command == "update":
            _podman_preflight(runner, image_required=arguments.image)
            if arguments.all_projects:
                root = _configured_state_root(environment)
                ensure_private_directory(root)
                projects_root = ensure_private_directory(root / "projects")
                project_ids = sorted(
                    path.name
                    for path in projects_root.iterdir()
                    if path.is_dir()
                    and not path.is_symlink()
                    and (path / "project.json").is_file()
                    and not (path / "project.json").is_symlink()
                )
                if not project_ids:
                    raise ValueError("no registered projects found")
            else:
                project_ids = [arguments.project]
            for project_id in project_ids:
                validate_project_id(project_id)
                layout = StateLayout.from_environment(project_id, environment)
                _ensure_exact_state_root(layout, environment)
                ensure_private_directory(layout.project_dir)
                ensure_private_directory(layout.codex_home)
                ensure_private_directory(layout.claude_config)
                _read_runtime_project(layout.project_file)
                _install_superpowers(layout, arguments.image, runner, update=True)
                print(
                    f"Updated Superpowers for project: {layout.project_id}",
                    file=stdout,
                )
            return 0
        if arguments.command == "stats":
            _podman_preflight(runner)
            running: list[tuple[str, str]] = []
            seen: set[str] = set()
            for agent in ("codex", "claude"):
                list_spec = podman_running_agent_containers_spec(
                    arguments.project, agent
                )
                listed = _required_probe_run(runner, list_spec)
                for container_id in (listed.stdout or "").splitlines():
                    container_id = container_id.strip()
                    podman_stats_spec(container_id)
                    if container_id in seen or len(running) >= 8:
                        raise ValueError("running agent container list is invalid")
                    seen.add(container_id)
                    running.append((agent, container_id))
            if not running:
                raise ValueError("no running agent container found for project")
            print("AGENT\tCONTAINER\tCPU\tMEMORY\tPIDS\tUPTIME", file=stdout)
            for agent, container_id in running:
                stats_spec = podman_stats_spec(container_id)
                completed = _required_probe_run(runner, stats_spec)
                body = completed.stdout or ""
                if (
                    len(body) > 512
                    or body.count("\n") != 1
                    or not body.endswith("\n")
                    or any(
                        ord(character) < 32 and character not in "\t\n"
                        for character in body
                    )
                ):
                    raise ValueError("container resource stats are invalid")
                fields = body.removesuffix("\n").split("\t")
                if len(fields) != 5 or fields[0] != container_id or any(
                    not field for field in fields
                ):
                    raise ValueError("container resource stats are invalid")
                print(agent + "\t" + "\t".join(fields), file=stdout)
            return 0
        if arguments.command == "run":
            layout, record, handover_project, uid, gid, egress_policy = _runtime_preflight(
                arguments.project,
                arguments.agent,
                environment,
                git_remote_reader,
                identity_reader,
                arguments.github_broker,
            )
            from agent_container.family_state import FamilyStateLayout
            from agent_container.family_state import load_family_binding

            family_layout = FamilyStateLayout(layout.root, layout.project_id)
            try:
                os.lstat(family_layout.family_binding_file)
            except FileNotFoundError:
                family_bound = False
            else:
                load_family_binding(family_layout.family_binding_file)
                family_bound = True
            if family_bound:
                _family_podman_preflight(runner, environment)
                image_probe = podman_image_exists_spec(arguments.image)
                _require_success(_required_probe_run(runner, image_probe), image_probe)
            else:
                _podman_preflight(runner, image_required=arguments.image)
            resolution = _resolve_project_image(
                layout,
                arguments.image,
                runner,
                build_missing=True,
                stdout=stdout,
            )
            if egress_policy is not None:
                egress_probe = egress_adapter_status_spec(resolution.image)
                _require_success(_suppressed_run(runner, egress_probe), egress_probe)
            if arguments.agent == "claude":
                policy_spec = claude_policy_status_spec(resolution.image)
                _require_success(_suppressed_run(runner, policy_spec), policy_spec)
                _prepare_claude_project_state(layout)
            builders = (
                {"codex": run_codex_spec, "claude": run_claude_spec}
                if runtime_spec_builders is None
                else runtime_spec_builders
            )
            if runtime_spec_builder is not None:
                builders = {**builders, "codex": runtime_spec_builder}
            with ExitStack() as stack:
                from agent_container.family_intake_runtime import FamilyIntakeRuntime

                if not family_bound:
                    family_runtime = None
                    family_mount = None
                else:
                    factory = (
                        FamilyIntakeRuntime.create
                        if family_runtime_factory is None
                        else family_runtime_factory
                    )
                    family_runtime = factory(
                        family_layout,
                        agent=arguments.agent,
                        repository=record.repository.name,
                    )
                    family_mount = stack.enter_context(family_runtime)
                egress_runtime = (
                    EgressBrokerRuntime.create(
                        layout, arguments.agent, egress_policy
                    )
                    if egress_policy is not None
                    else None
                )
                egress_mount = (
                    stack.enter_context(egress_runtime)
                    if egress_runtime is not None
                    else None
                )
                github_mount = (
                    stack.enter_context(
                        UploadPackBrokerRuntime.create(layout, record)
                    )
                    if arguments.github_broker
                    else None
                )
                handover_mount = (
                    stack.enter_context(
                        HandoverBrokerRuntime.create(layout, handover_project)
                    )
                    if arguments.agent == "claude"
                    else None
                )
                if arguments.agent == "claude":
                    assert handover_mount is not None
                    builder_args = [
                        layout,
                        handover_project,
                        resolution.image,
                        uid,
                        gid,
                        handover_mount,
                    ]
                    if github_mount is not None or egress_mount is not None:
                        builder_args.append(github_mount)
                    if egress_mount is not None:
                        builder_args.append(egress_mount)
                    spec = (
                        builders[arguments.agent](
                            *builder_args, family_mount=family_mount
                        )
                        if family_mount is not None
                        else builders[arguments.agent](*builder_args)
                    )
                else:
                    builder_args = [
                        layout,
                        handover_project,
                        resolution.image,
                        uid,
                        gid,
                    ]
                    if github_mount is not None or egress_mount is not None:
                        builder_args.append(github_mount)
                    if egress_mount is not None:
                        builder_args.append(egress_mount)
                    spec = (
                        builders[arguments.agent](
                            *builder_args, family_mount=family_mount
                        )
                        if family_mount is not None
                        else builders[arguments.agent](*builder_args)
                    )
                print(
                    f"Starting {arguments.agent.title()} for project: {layout.project_id}",
                    file=stdout,
                )
                if family_runtime is not None:
                    supervisor = (
                        run_command_supervised
                        if runtime_supervisor is None
                        else runtime_supervisor
                    )
                    completed = supervisor(
                        spec,
                        egress_runtime,
                        egress_mount,
                        family_runtime,
                        family_mount,
                    )
                elif egress_runtime is not None and egress_mount is not None:
                    completed = (
                        run_command_supervised(spec, egress_runtime, egress_mount)
                        if runner is run_command
                        else _run_with_egress_supervision(
                            runner, spec, egress_runtime, egress_mount
                        )
                    )
                else:
                    completed = runner(spec)
                _require_success(completed, spec)
            return 0
        if arguments.command == "doctor":
            checks = _doctor(
                arguments.project,
                arguments.agent,
                arguments.image,
                environment,
                runner,
                git_remote_reader,
                arguments.github_broker,
            )
            for check in checks:
                print(
                    f"{check.level}  {check.name}: {check.detail}",
                    file=stdout,
                )
            return 1 if any(check.level == "FAIL" for check in checks) else 0
        if arguments.command == "migrate":
            layout = StateLayout.from_environment(arguments.project, environment)
            _ensure_exact_state_root(layout, environment)
            for directory in (
                layout.root,
                layout.project_dir.parent,
                layout.project_dir,
            ):
                ensure_private_directory(directory)
            _read_runtime_project(layout.project_file)
            plan = plan_claude_migration(arguments.source, layout.claude_config)
            plan = add_plugin_entries(plan, tuple(arguments.plugins))
            rendered = render_migration_plan(plan)
            if arguments.apply:
                apply_claude_migration(plan)
            for line in rendered:
                print(line, file=stdout)
            print(f"MODE {'apply' if arguments.apply else 'dry-run'}", file=stdout)
            return 0
        return 1
    except subprocess.CalledProcessError as error:
        print(f"error: command failed with exit code {error.returncode}", file=stderr)
        return error.returncode or 1
    except _ClaudeTokenFilesystemError:
        print("error: Claude token filesystem operation failed", file=stderr)
        return 1
    except _ClaudeRuntimeStateError:
        print("error: Claude runtime state validation failed", file=stderr)
        return 1
    except GitHubBrokerRuntimeError:
        print("error: GitHub broker failed", file=stderr)
        return 1
    except HandoverBrokerRuntimeError:
        print("error: handover broker failed", file=stderr)
        return 1
    except EgressBrokerRuntimeError:
        print("error: egress gateway failed", file=stderr)
        return 1
    except FamilyRuntimeError:
        print("error: family intake runtime failed", file=stderr)
        return 1
    except (ValueError, PermissionError, FileNotFoundError) as error:
        print(f"error: {error}", file=stderr)
        return 1
    except OSError:
        print("error: filesystem operation failed", file=stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
