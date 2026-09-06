from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import secrets
import stat

from agent_container.broker.peer import SameUser
from agent_container.broker.runtime import Connection
from agent_container.broker.runtime import SocketBrokerRuntime
from agent_container.github_app import GitHubAppMetadata
from agent_container.github_app import InstallationTokenProvider
from agent_container.github_broker import BrokerSession
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_broker_transport import handle_broker_connection
from agent_container.github_git_transport import GitHubUploadPackTransport
from agent_container.github_git_transport import GitHubReceivePackTransport
from agent_container.github_issue import GitHubIssueTransport
from agent_container.github_pr import GitHubPullRequestTransport
from agent_container.podman import BrokerRuntimeMount
from agent_container.state import ProjectRecord
from agent_container.state import StateLayout
from agent_container.state import ensure_private_file


class GitHubBrokerRuntimeError(Exception):
    pass


_LISTENER_TIMEOUT_SECONDS = 0.2
_CLIENT_TIMEOUT_SECONDS = 30
_STOP_TIMEOUT_SECONDS = 2
_LISTENER_BACKLOG = 4


def broker_token_metadata(
    app: GitHubAppMetadata, policy: BrokerPolicy
) -> GitHubAppMetadata:
    repository_id = (
        app.repository_id
        if policy.repository_id is None
        else policy.repository_id
    )
    return replace(app, repository_id=repository_id)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("GitHub broker policy is invalid")
        result[key] = value
    return result


def _decode_broker_policy(
    body: bytes, record: ProjectRecord, project_id: str
) -> BrokerPolicy:
    try:
        payload = json.loads(
            body.decode("utf-8"), object_pairs_hook=_unique_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("GitHub broker policy is invalid") from None
    base_keys = {"repository", "default_branch", "protected_branches"}
    new_bound_keys = base_keys | {"repository_id"}
    legacy_global_keys = base_keys | {"ruleset_confirmed"}
    legacy_bound_keys = new_bound_keys | {"ruleset_confirmed"}
    if not isinstance(payload, dict) or set(payload) not in (
        new_bound_keys,
        legacy_global_keys,
        legacy_bound_keys,
    ):
        raise ValueError("GitHub broker policy is invalid")
    if payload["repository"] != record.repository.slug:
        raise ValueError("GitHub broker policy repository does not match project")
    if (
        set(payload) in (legacy_global_keys, legacy_bound_keys)
        and payload["ruleset_confirmed"] is not True
    ):
        raise ValueError("GitHub broker force-push ruleset is not confirmed")
    protected = payload["protected_branches"]
    if (
        not isinstance(payload["default_branch"], str)
        or not isinstance(protected, list)
        or not all(isinstance(branch, str) for branch in protected)
    ):
        raise ValueError("GitHub broker policy is invalid")
    return BrokerPolicy.create(
        project_id=project_id,
        repository=record.repository.slug,
        repository_id=payload.get("repository_id"),
        default_branch=payload["default_branch"],
        protected_branches=protected,
        require_repository_id="repository_id" in payload,
    )


def load_broker_policy(path: Path, record: ProjectRecord, project_id: str) -> BrokerPolicy:
    ensure_private_file(path)
    return _decode_broker_policy(path.read_bytes(), record, project_id)


def _encode_broker_policy(policy: BrokerPolicy) -> bytes:
    if policy.repository_id is None:
        raise ValueError("GitHub repository ID is invalid")
    body = json.dumps(
        {
            "repository": policy.repository.slug,
            "repository_id": policy.repository_id,
            "default_branch": policy.default_branch,
            "protected_branches": sorted(policy.protected_branches),
        },
        ensure_ascii=True,
        indent=2,
    ) + "\n"
    return body.encode("ascii")


def _write_all(descriptor: int, body: bytes) -> None:
    remaining = memoryview(body)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("GitHub broker policy write failed")
        remaining = remaining[written:]


def write_broker_policy(path: Path, policy: BrokerPolicy) -> None:
    body = _encode_broker_policy(policy)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        _write_all(descriptor, body)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_private_directory_stat(directory_stat: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or stat.S_IMODE(directory_stat.st_mode) != 0o700
        or directory_stat.st_uid != os.getuid()
    ):
        raise PermissionError("GitHub broker policy parent is not private")


def _validate_policy_parent_identity(
    directory: Path, expected: tuple[int, int]
) -> None:
    try:
        canonical = directory.resolve(strict=True)
        path_stat = os.stat(directory, follow_symlinks=False)
        canonical_stat = os.stat(canonical, follow_symlinks=False)
    except OSError:
        raise ValueError("GitHub broker policy parent changed during upgrade") from None
    _validate_private_directory_stat(path_stat)
    if (
        not stat.S_ISDIR(path_stat.st_mode)
        or (path_stat.st_dev, path_stat.st_ino) != expected
        or (canonical_stat.st_dev, canonical_stat.st_ino) != expected
    ):
        raise ValueError("GitHub broker policy parent changed during upgrade")


def _legacy_policy_snapshot(
    parent_descriptor: int, name: str, expected: BrokerPolicy
) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
    path_stat = os.stat(
        name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    if not stat.S_ISREG(path_stat.st_mode):
        raise ValueError("GitHub broker policy must not be a symlink")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or stat.S_IMODE(file_stat.st_mode) != 0o600
            or file_stat.st_uid != os.getuid()
        ):
            raise PermissionError("GitHub broker policy is not a private file")
        if (
            path_stat.st_dev != file_stat.st_dev
            or path_stat.st_ino != file_stat.st_ino
        ):
            raise ValueError("GitHub broker policy changed during upgrade")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            chunks.append(chunk)
        body = b"".join(chunks)
    finally:
        os.close(descriptor)
    path_stat = os.stat(
        name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or path_stat.st_dev != file_stat.st_dev
        or path_stat.st_ino != file_stat.st_ino
    ):
        raise ValueError("GitHub broker policy changed during upgrade")
    record = ProjectRecord(expected.repository, Path("/"))
    if _decode_broker_policy(body, record, expected.project_id) != expected:
        raise ValueError("GitHub broker policy does not match observed policy")
    identity = (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
        file_stat.st_mode,
    )
    return body, identity


def _open_policy_temporary(
    parent_descriptor: int, policy_name: str
) -> tuple[str, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    for _ in range(128):
        temporary = f".{policy_name}.{secrets.token_hex(16)}"
        try:
            return temporary, os.open(
                temporary,
                flags,
                0o600,
                dir_fd=parent_descriptor,
            )
        except FileExistsError:
            continue
    raise FileExistsError("could not create GitHub broker policy temporary file")


def upgrade_legacy_broker_policy(
    path: Path, existing: BrokerPolicy, requested: BrokerPolicy
) -> None:
    if (
        existing.repository_id is not None
        or requested.repository_id is None
        or replace(requested, repository_id=None) != existing
    ):
        raise ValueError("GitHub broker policy is not an exact legacy match")
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    parent_descriptor = os.open(path.parent, directory_flags)
    temporary: str | None = None
    descriptor: int | None = None
    replaced = False
    try:
        parent_stat = os.fstat(parent_descriptor)
        _validate_private_directory_stat(parent_stat)
        parent_identity = (parent_stat.st_dev, parent_stat.st_ino)
        _validate_policy_parent_identity(path.parent, parent_identity)
        original = _legacy_policy_snapshot(
            parent_descriptor, path.name, existing
        )
        body = _encode_broker_policy(requested)
        temporary, descriptor = _open_policy_temporary(
            parent_descriptor, path.name
        )
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, body)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if (
            _legacy_policy_snapshot(parent_descriptor, path.name, existing)
            != original
        ):
            raise ValueError("GitHub broker policy changed during upgrade")
        _validate_policy_parent_identity(path.parent, parent_identity)
        os.replace(
            temporary,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        replaced = True
        os.fsync(parent_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None and not replaced:
            try:
                os.unlink(temporary, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        os.close(parent_descriptor)


@dataclass
class UploadPackBrokerRuntime(AbstractContextManager[BrokerRuntimeMount]):
    session: BrokerSession
    transport: GitHubUploadPackTransport
    receive_transport: GitHubReceivePackTransport | None = None
    pr_transport: GitHubPullRequestTransport | None = None
    issue_transport: GitHubIssueTransport | None = None
    _runtime: SocketBrokerRuntime = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._runtime = SocketBrokerRuntime(
            label="GitHub broker",
            thread_name="github-broker",
            open_listener=lambda backlog: self.session.open_listener(backlog=backlog),
            handler=self._handle,
            deactivate=lambda: self.session.deactivate(),
            close=lambda: self.session.close(),
            error_type=GitHubBrokerRuntimeError,
            peer_policy=SameUser(),
            backlog=_LISTENER_BACKLOG,
            listener_timeout=_LISTENER_TIMEOUT_SECONDS,
            client_timeout=_CLIENT_TIMEOUT_SECONDS,
            # Revoke only after the join so in-flight git/PR handlers can still
            # audit; GitHub never revoked before the join in stage 1, and the
            # kernel still revokes before the did-not-stop report.
            deactivate_after_join=True,
        )

    @classmethod
    def create(cls, layout: StateLayout, record: ProjectRecord) -> "UploadPackBrokerRuntime":
        policy = load_broker_policy(
            layout.github_broker_policy_file, record, layout.project_id
        )
        metadata = GitHubAppMetadata.load(
            layout.github_broker_root / "app.json",
            layout.github_broker_root / "private-key.pem",
        )
        session = BrokerSession.create(layout.root, policy)
        tokens = InstallationTokenProvider(broker_token_metadata(metadata, policy))
        return cls(
            session,
            GitHubUploadPackTransport(record.repository, tokens),
            GitHubReceivePackTransport(record.repository, tokens),
            GitHubPullRequestTransport(policy, tokens),
            GitHubIssueTransport(policy, tokens),
        )

    def _handle(self, connection: Connection) -> int:
        return handle_broker_connection(
            self.session,
            connection.stream,
            self.transport,
            self.receive_transport,
            self.pr_transport,
            self.issue_transport,
        )

    def __enter__(self) -> BrokerRuntimeMount:
        self._runtime.start()
        return BrokerRuntimeMount(self.session.run_dir, self.session.policy.repository)

    def __exit__(self, *_: object) -> None:
        self._runtime.stop(join_timeout=_STOP_TIMEOUT_SECONDS)
