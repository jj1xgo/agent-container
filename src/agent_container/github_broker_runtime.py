from contextlib import AbstractContextManager
from dataclasses import dataclass, field
import json
from pathlib import Path
import socket
import threading

from agent_container.github_app import GitHubAppMetadata
from agent_container.github_app import InstallationTokenProvider
from agent_container.github_broker import BrokerSession
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.github_broker_transport import handle_upload_pack_connection
from agent_container.github_git_transport import GitHubUploadPackTransport
from agent_container.podman import BrokerRuntimeMount
from agent_container.state import ProjectRecord
from agent_container.state import StateLayout
from agent_container.state import ensure_private_file


class GitHubBrokerRuntimeError(Exception):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("GitHub broker policy is invalid")
        result[key] = value
    return result


def load_broker_policy(path: Path, record: ProjectRecord, project_id: str) -> BrokerPolicy:
    ensure_private_file(path)
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("GitHub broker policy is invalid") from None
    if not isinstance(payload, dict) or set(payload) != {
        "repository",
        "default_branch",
        "protected_branches",
        "ruleset_confirmed",
    }:
        raise ValueError("GitHub broker policy is invalid")
    if payload["repository"] != record.repository.slug:
        raise ValueError("GitHub broker policy repository does not match project")
    if payload["ruleset_confirmed"] is not True:
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
        default_branch=payload["default_branch"],
        protected_branches=protected,
    )


@dataclass
class UploadPackBrokerRuntime(AbstractContextManager[BrokerRuntimeMount]):
    session: BrokerSession
    transport: GitHubUploadPackTransport
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _error: BaseException | None = field(default=None, init=False, repr=False)

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
        tokens = InstallationTokenProvider(metadata)
        return cls(session, GitHubUploadPackTransport(record.repository, tokens))

    def __enter__(self) -> BrokerRuntimeMount:
        try:
            listener = self.session.open_listener()
            listener.settimeout(0.2)
            self._thread = threading.Thread(
                target=self._serve,
                args=(listener,),
                name="github-broker",
                daemon=True,
            )
            self._thread.start()
        except Exception:
            self.session.close()
            raise
        return BrokerRuntimeMount(self.session.run_dir, self.session.policy.repository)

    def _serve(self, listener: socket.socket) -> None:
        try:
            while not self._stop.is_set():
                try:
                    client, _ = listener.accept()
                except TimeoutError:
                    continue
                with client:
                    stream = client.makefile("rwb", buffering=0)
                    try:
                        handle_upload_pack_connection(
                            self.session, stream, self.transport
                        )
                    finally:
                        stream.close()
        except BaseException as error:
            if not self._stop.is_set():
                self._error = error

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self.session._listener is not None:
            self.session._listener.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise GitHubBrokerRuntimeError("GitHub broker did not stop")
        self.session.close()
        if self._error is not None:
            raise GitHubBrokerRuntimeError("GitHub broker failed") from None
