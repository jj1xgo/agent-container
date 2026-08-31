from dataclasses import dataclass
import os
from pathlib import Path
import re
import signal
import subprocess

from agent_container.handover_broker_runtime import HandoverRuntimeMount
from agent_container.family_intake_runtime import FamilyRuntimeMount
from agent_container.egress_broker_runtime import EgressBrokerRuntime
from agent_container.egress_broker_runtime import EgressBrokerRuntimeError
from agent_container.egress_broker_runtime import EgressRuntimeMount
from agent_container.state import Repository
from agent_container.state import StateLayout
from agent_container.state import validate_project_id
from agent_container.state import github_broker_project_label


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
_HANDOVER_BROKER_RUNTIME_PATH = "/run/agent-handover"
_EGRESS_RUNTIME_PATH = "/run/agent-egress"
_FAMILY_RUNTIME_PATH = "/run/agent-family"
_FAMILY_RUN_ID = re.compile(r"^[0-9a-f]{16}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
_RESOURCE_AGENTS = frozenset({"codex", "claude"})
_RESOURCE_STATS_FORMAT = (
    "{{.ID}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.PIDs}}\t{{.UpTime}}"
)


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    environment: dict[str, str]


@dataclass(frozen=True)
class BrokerRuntimeMount:
    run_dir: Path
    repository: Repository


def validate_claude_handover_project(
    layout: StateLayout, handover_project: Path
) -> None:
    resolved_handover = handover_project.resolve()
    for writable_source in (
        layout.root,
        layout.workspace,
        layout.claude_config,
        layout.cache,
    ):
        resolved_writable = writable_source.resolve()
        if (
            resolved_handover == resolved_writable
            or resolved_handover.is_relative_to(resolved_writable)
            or resolved_writable.is_relative_to(resolved_handover)
        ):
            raise ValueError(
                "Claude handover project must not overlap agent state or a writable mount"
            )


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


def _runtime_monitor_args(layout: StateLayout, agent: str) -> list[str]:
    if agent not in {"codex", "claude"}:
        raise ValueError("runtime agent is invalid")
    return [
        "--label",
        "io.agent-container.managed=true",
        "--label",
        f"io.agent-container.project={layout.project_id}",
        "--label",
        f"io.agent-container.agent={agent}",
    ]


def _egress_args(
    layout: StateLayout, agent: str, egress: EgressRuntimeMount
) -> list[str]:
    if (
        not egress.run_dir.is_absolute()
        or egress.project_id != layout.project_id
        or egress.agent != agent
    ):
        raise ValueError("egress runtime mount does not match agent runtime")
    proxy = "http://127.0.0.1:17843"
    arguments = [
        f"--name={egress.container_name}",
        "--network=none",
        "--mount",
        _mount(egress.run_dir, _EGRESS_RUNTIME_PATH, True),
        "--env",
        f"AGENT_EGRESS_SOCKET={_EGRESS_RUNTIME_PATH}/broker.sock",
        "--env",
        f"AGENT_EGRESS_CAPABILITY={_EGRESS_RUNTIME_PATH}/capability",
        "--env",
        f"AGENT_EGRESS_AGENT={agent}",
    ]
    for name, value in (
        ("HTTPS_PROXY", proxy),
        ("https_proxy", proxy),
        ("HTTP_PROXY", proxy),
        ("http_proxy", proxy),
        ("NO_PROXY", "localhost,127.0.0.1,::1"),
        ("no_proxy", "localhost,127.0.0.1,::1"),
    ):
        arguments += ["--env", f"{name}={value}"]
    return arguments


def egress_runtime_stop_spec(egress: EgressRuntimeMount) -> CommandSpec:
    return CommandSpec(
        ("podman", "stop", "--ignore", "--time=2", egress.container_name), {}
    )


def egress_runtime_kill_spec(egress: EgressRuntimeMount) -> CommandSpec:
    return CommandSpec(
        ("podman", "kill", "--signal=KILL", egress.container_name), {}
    )


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
    github_url = broker.repository.https_url
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


def _handover_broker_args(broker: HandoverRuntimeMount) -> list[str]:
    if not broker.run_dir.is_absolute():
        raise ValueError("handover broker runtime path must be absolute")
    return [
        "--mount",
        _mount(broker.run_dir, _HANDOVER_BROKER_RUNTIME_PATH, True),
        "--env",
        f"AGENT_HANDOVER_BROKER_SOCKET={_HANDOVER_BROKER_RUNTIME_PATH}/broker.sock",
        "--env",
        f"AGENT_HANDOVER_BROKER_CAPABILITY={_HANDOVER_BROKER_RUNTIME_PATH}/capability",
    ]


def _family_runtime_args(
    layout: StateLayout, family: FamilyRuntimeMount
) -> list[str]:
    if type(family) is not FamilyRuntimeMount:
        raise ValueError("family runtime mount is invalid")
    expected_parent = (
        layout.root
        / "family"
        / "intake"
        / "r"
        / github_broker_project_label(layout.project_id)
    )
    socket_dir = family.socket_dir
    expected_environment = {
        "AGENT_FAMILY_SOCKET": f"{_FAMILY_RUNTIME_PATH}/intake.sock",
        "AGENT_FAMILY_CAPABILITY": family.capability,
    }
    if (
        not socket_dir.is_absolute()
        or socket_dir.parent != expected_parent
        or _FAMILY_RUN_ID.fullmatch(socket_dir.name) is None
        or socket_dir != Path(os.path.normpath(socket_dir))
        or dict(family.environment) != expected_environment
    ):
        raise ValueError("family runtime mount is invalid")
    return [
        "--mount",
        _mount(socket_dir, _FAMILY_RUNTIME_PATH),
        "--env",
        f"AGENT_FAMILY_SOCKET={expected_environment['AGENT_FAMILY_SOCKET']}",
        "--env",
        f"AGENT_FAMILY_CAPABILITY={family.capability}",
    ]


def build_image_spec(
    repo_root: Path,
    image: str,
    node_version: str,
    codex_version: str,
    claude_version: str,
    cachebuster: str,
    agent_container_version: str,
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
            "--build-arg",
            f"AGENT_CONTAINER_VERSION={agent_container_version}",
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


def podman_running_agent_containers_spec(
    project_id: str, agent: str
) -> CommandSpec:
    project_id = validate_project_id(project_id)
    if agent not in _RESOURCE_AGENTS:
        raise ValueError("runtime agent is invalid")
    return CommandSpec(
        (
            "podman",
            "ps",
            "--filter",
            "label=io.agent-container.managed=true",
            "--filter",
            f"label=io.agent-container.project={project_id}",
            "--filter",
            f"label=io.agent-container.agent={agent}",
            "--format",
            "{{.ID}}",
        ),
        {},
    )


def podman_stats_spec(container_id: str) -> CommandSpec:
    if _CONTAINER_ID.fullmatch(container_id) is None:
        raise ValueError("container ID is invalid")
    return CommandSpec(
        (
            "podman",
            "stats",
            "--no-stream",
            "--format",
            _RESOURCE_STATS_FORMAT,
            container_id,
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


def handover_broker_client_status_spec(image: str) -> CommandSpec:
    argv = _noninteractive_prefix(os.getuid(), os.getgid())
    argv += [
        image,
        "python3",
        "-m",
        "agent_container.handover_broker_client",
        "--self-check",
    ]
    return CommandSpec(tuple(argv), {})


def egress_adapter_status_spec(image: str) -> CommandSpec:
    argv = _noninteractive_prefix(os.getuid(), os.getgid())
    argv += ["--network=none"]
    argv += [image, "agent-egress-runtime", "--self-check"]
    return CommandSpec(tuple(argv), {})


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
    egress: EgressRuntimeMount | None = None,
    family_mount: FamilyRuntimeMount | None = None,
) -> CommandSpec:
    if uid != os.getuid() or gid != os.getgid():
        raise ValueError("runtime uid and gid must match the current user")
    argv = _runtime_prefix(uid, gid)
    argv += _runtime_monitor_args(layout, "codex")
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
    if egress is not None:
        argv += _egress_args(layout, "codex", egress)
    if family_mount is not None:
        argv += _family_runtime_args(layout, family_mount)
    argv += ["--env", f"AGENT_PROJECT_ID={layout.project_id}", image]
    if egress is not None:
        argv += ["agent-egress-runtime", "--"]
    argv += [
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
    handover_broker: HandoverRuntimeMount,
    broker: BrokerRuntimeMount | None = None,
    egress: EgressRuntimeMount | None = None,
    family_mount: FamilyRuntimeMount | None = None,
) -> CommandSpec:
    if uid != os.getuid() or gid != os.getgid():
        raise ValueError("runtime uid and gid must match the current user")
    validate_claude_handover_project(layout, handover_project)
    gh_config_dir = "/home/agent/gh-config"
    argv = _runtime_prefix(uid, gid)
    argv += _runtime_monitor_args(layout, "claude")
    argv += ["--mount", _CLAUDE_RUNTIME_HOME_TMPFS_MOUNT]
    argv += (
        _git_environment_args(gh_config_dir)
        if broker is None
        else _broker_git_args(layout, broker)
    )
    argv += _handover_broker_args(handover_broker)
    mounts = [
        (layout.workspace, "/workspace", False),
        (layout.claude_config, "/home/agent/.claude", False),
        (layout.claude_token_file, _CLAUDE_TOKEN_PATH, True),
        (layout.cache, "/home/agent/.cache", False),
        (handover_project, f"/handovers/{layout.project_id}", True),
    ]
    if broker is None:
        mounts.insert(-1, (layout.gh_dir, gh_config_dir, True))
    for source, target, read_only in mounts:
        argv += ["--mount", _mount(source, target, read_only)]
    argv += ["--env", "CLAUDE_CONFIG_DIR=/home/agent/.claude"]
    argv += ["--env", "AGENT_HANDOVER_ROOT=/handovers"]
    if egress is not None:
        argv += _egress_args(layout, "claude", egress)
    if family_mount is not None:
        argv += _family_runtime_args(layout, family_mount)
    argv += ["--env", f"AGENT_PROJECT_ID={layout.project_id}", image]
    if egress is not None:
        argv += ["agent-egress-runtime", "--"]
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


def run_command_supervised(
    spec: CommandSpec,
    gateway: EgressBrokerRuntime | None,
    egress: EgressRuntimeMount | None,
    family_runtime=None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(spec.environment)
    interrupted: list[int] = []

    def handle_signal(signum, _frame) -> None:
        interrupted.append(signum)

    watched_signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous_handlers = {
        signum: signal.getsignal(signum) for signum in watched_signals
    }
    for signum in watched_signals:
        signal.signal(signum, handle_signal)
    process = None
    gateway_failed = False
    failure: BaseException | None = None
    try:
        launch_argv = spec.argv
        if family_runtime is not None:
            launch_argv = (
                "/bin/sh",
                "-c",
                'kill -STOP $$; exec "$@"',
                "agent-runtime",
                *spec.argv,
            )
        process = subprocess.Popen(launch_argv, env=environment, text=True)
        if family_runtime is not None:
            try:
                stopped_pid, stopped_status = os.waitpid(process.pid, os.WUNTRACED)
                if (
                    stopped_pid != process.pid
                    or not os.WIFSTOPPED(stopped_status)
                    or os.WSTOPSIG(stopped_status) != signal.SIGSTOP
                ):
                    raise RuntimeError("family runtime launch handoff failed")
                family_runtime.register_runtime(process.pid)
            except BaseException:
                try:
                    os.kill(process.pid, signal.SIGKILL)
                finally:
                    try:
                        process.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                process = None
                raise
            os.kill(process.pid, signal.SIGCONT)
        while process.poll() is None and not interrupted:
            if gateway is not None and gateway.wait_failed(0.1):
                gateway_failed = True
                break
            if family_runtime is not None:
                family_runtime.check()
    except BaseException as error:
        failure = error
    finally:
        for signum in watched_signals:
            signal.signal(signum, previous_handlers[signum])

    if process is None:
        assert failure is not None
        raise failure
    if gateway_failed or interrupted or failure is not None:
        _reap_process(process)
        if egress is not None:
            _stop_egress_container(egress)
        if failure is not None:
            raise failure
        raise EgressBrokerRuntimeError("egress gateway failed")
    try:
        returncode = process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        _reap_process(process)
        if egress is not None:
            _stop_egress_container(egress)
        raise EgressBrokerRuntimeError("egress gateway failed") from None
    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, spec.argv)
    return subprocess.CompletedProcess(spec.argv, returncode)


def _reap_process(process: subprocess.Popen[str]) -> None:
    try:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _stop_egress_container(egress: EgressRuntimeMount) -> None:
    stop = egress_runtime_stop_spec(egress)
    try:
        completed = subprocess.run(
            stop.argv,
            env={**os.environ, **stop.environment},
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        completed = None
    if completed is not None and completed.returncode == 0:
        return
    kill = egress_runtime_kill_spec(egress)
    try:
        completed = subprocess.run(
            kill.argv,
            env={**os.environ, **kill.environment},
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise EgressBrokerRuntimeError("egress runtime cleanup failed") from error
    if completed.returncode != 0:
        raise EgressBrokerRuntimeError("egress runtime cleanup failed")
