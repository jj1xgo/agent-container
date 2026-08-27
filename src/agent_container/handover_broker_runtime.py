from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
import socket
import struct
import threading
from typing import BinaryIO

from agent_container.handover_broker import HandoverBrokerSession
from agent_container.handover_broker_transport import handle_handover_connection
from agent_container.state import StateLayout


_LISTENER_TIMEOUT_SECONDS = 0.2
_CLIENT_TIMEOUT_SECONDS = 30
_STOP_TIMEOUT_SECONDS = 2
_LISTENER_BACKLOG = 4
_PEER_CREDENTIAL_BYTES = 12


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
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _listener: socket.socket | None = field(default=None, init=False, repr=False)
    _error: BaseException | None = field(default=None, init=False, repr=False)
    _exited: bool = field(default=False, init=False, repr=False)

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

    def __enter__(self) -> HandoverRuntimeMount:
        if self._thread is not None or self._exited:
            raise HandoverBrokerRuntimeError("handover broker failed to start")
        listener: socket.socket | None = None
        try:
            listener = self.session.open_listener(backlog=_LISTENER_BACKLOG)
            listener.settimeout(_LISTENER_TIMEOUT_SECONDS)
            self._listener = listener
            thread = threading.Thread(
                target=self._serve,
                args=(listener,),
                name="handover-broker",
                daemon=True,
            )
            thread.start()
            self._thread = thread
        except BaseException:
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass
            cleanup_complete = False
            try:
                self.session.close()
            except (OSError, ValueError):
                pass
            else:
                cleanup_complete = True
            self._exited = cleanup_complete
            raise HandoverBrokerRuntimeError(
                "handover broker failed to start"
            ) from None
        return HandoverRuntimeMount(self.session.run_dir)

    def _serve(self, listener: socket.socket) -> None:
        try:
            while not self._stop.is_set():
                try:
                    client, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                with client:
                    client.settimeout(_CLIENT_TIMEOUT_SECONDS)
                    credentials = client.getsockopt(
                        socket.SOL_SOCKET,
                        socket.SO_PEERCRED,
                        _PEER_CREDENTIAL_BYTES,
                    )
                    _pid, peer_uid, _gid = struct.unpack("3i", credentials)
                    stream: BinaryIO = client.makefile("rwb", buffering=0)
                    try:
                        handle_handover_connection(
                            self.session,
                            stream,
                            peer_uid,
                        )
                    finally:
                        stream.close()
        except BaseException as error:
            self._error = error

    def __exit__(self, *_: object) -> None:
        if self._exited:
            return
        self._stop.set()
        self.session.deactivate()
        cleanup_failed = False
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                cleanup_failed = True
            else:
                self._listener = None

        did_not_stop = False
        if self._thread is not None:
            self._thread.join(timeout=_STOP_TIMEOUT_SECONDS)
            did_not_stop = self._thread.is_alive()

        if did_not_stop:
            raise HandoverBrokerRuntimeError(
                "handover broker did not stop"
            ) from None

        try:
            self.session.close()
        except (OSError, ValueError):
            cleanup_failed = True
        else:
            self._exited = True

        if cleanup_failed:
            raise HandoverBrokerRuntimeError(
                "handover broker cleanup failed"
            ) from None
        if self._error is not None:
            raise HandoverBrokerRuntimeError("handover broker failed") from None
