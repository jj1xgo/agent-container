from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
import threading
from typing import Any

from agent_container.broker.runtime import Connection
from agent_container.broker.runtime import SocketBrokerRuntime
from agent_container.handover_broker import HandoverBrokerSession
from agent_container.handover_broker_transport import handle_handover_connection
from agent_container.state import StateLayout


_LISTENER_TIMEOUT_SECONDS = 0.2
_CLIENT_TIMEOUT_SECONDS = 30
_STOP_TIMEOUT_SECONDS = 2
_LISTENER_BACKLOG = 4


class HandoverBrokerRuntimeError(Exception):
    pass


@dataclass(frozen=True)
class HandoverRuntimeMount:
    run_dir: Path

    @property
    def socket_path(self) -> Path:
        return self.run_dir / "broker.sock"

    @property
    def capability_path(self) -> Path:
        return self.run_dir / "capability"


@dataclass
class HandoverBrokerRuntime(AbstractContextManager[HandoverRuntimeMount]):
    session: HandoverBrokerSession
    _runtime: SocketBrokerRuntime = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._runtime = SocketBrokerRuntime(
            label="handover broker",
            thread_name="handover-broker",
            open_listener=lambda backlog: self.session.open_listener(backlog=backlog),
            handler=self._handle,
            deactivate=lambda: self.session.deactivate(),
            close=lambda: self.session.close(),
            error_type=HandoverBrokerRuntimeError,
            backlog=_LISTENER_BACKLOG,
            listener_timeout=_LISTENER_TIMEOUT_SECONDS,
            client_timeout=_CLIENT_TIMEOUT_SECONDS,
        )

    @classmethod
    def create(
        cls,
        layout: StateLayout,
        project_dir: Path,
    ) -> "HandoverBrokerRuntime":
        session = HandoverBrokerSession.create(
            layout.root,
            layout.project_id,
            project_dir,
        )
        return cls(session)

    def _handle(self, connection: Connection) -> int:
        return handle_handover_connection(
            self.session, connection.stream, connection.peer_uid
        )

    @property
    def _thread(self) -> Any | None:
        return self._runtime.thread

    @property
    def _stop(self) -> threading.Event:
        return self._runtime.stop_event

    def __enter__(self) -> HandoverRuntimeMount:
        self._runtime.start()
        return HandoverRuntimeMount(self.session.run_dir)

    def __exit__(self, *_: object) -> None:
        self._runtime.stop(join_timeout=_STOP_TIMEOUT_SECONDS)
