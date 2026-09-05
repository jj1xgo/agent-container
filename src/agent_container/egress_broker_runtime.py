from contextlib import AbstractContextManager
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import socket
import threading
from typing import Any
from typing import BinaryIO

from agent_container.broker.runtime import SocketBrokerRuntime
from agent_container.broker.runtime import open_connection
from agent_container.egress_broker import EgressBrokerSession
from agent_container.egress_broker_protocol import EgressResponse
from agent_container.egress_broker_protocol import encode_response_frame
from agent_container.egress_broker_protocol import read_request_frame
from agent_container.egress_gateway import connect_target
from agent_container.egress_gateway import RelayLimits
from agent_container.egress_gateway import relay_tunnel
from agent_container.egress_gateway import resolve_target
from agent_container.egress_policy import EgressPolicy
from agent_container.state import StateLayout


_LISTENER_TIMEOUT_SECONDS = 0.2
_STOP_TIMEOUT_SECONDS = 2
_LISTENER_BACKLOG = 32
_MAX_ACTIVE_TUNNELS = 32
_MAX_CREATED_TUNNELS = 128
_CLIENT_TIMEOUT_SECONDS = 30


class EgressBrokerRuntimeError(Exception):
    pass


@dataclass(frozen=True)
class EgressRuntimeMount:
    run_dir: Path
    project_id: str
    agent: str

    @property
    def socket_path(self) -> Path:
        return self.run_dir / "broker.sock"

    @property
    def capability_path(self) -> Path:
        return self.run_dir / "capability"

    @property
    def container_name(self) -> str:
        label = hashlib.sha256(str(self.run_dir).encode("utf-8")).hexdigest()[:16]
        return f"agent-egress-{label}"


@dataclass
class EgressBrokerRuntime(AbstractContextManager[EgressRuntimeMount]):
    session: EgressBrokerSession
    _runtime: SocketBrokerRuntime = field(init=False, repr=False)
    _limit_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _next_reservation: int = field(default=1, init=False, repr=False)
    _active_reservations: set[int] = field(default_factory=set, init=False, repr=False)
    _created_reservations: set[int] = field(default_factory=set, init=False, repr=False)
    _created_tunnels: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._runtime = SocketBrokerRuntime(
            label="egress broker",
            thread_name="egress-broker",
            open_listener=lambda backlog: self.session.open_listener(backlog=backlog),
            # Looked up per call so tests can patch _handle_client on the instance.
            handler=lambda client: self._handle_client(client),
            deactivate=lambda: self.session.deactivate(),
            close=lambda: self.session.close(),
            error_type=EgressBrokerRuntimeError,
            backlog=_LISTENER_BACKLOG,
            listener_timeout=_LISTENER_TIMEOUT_SECONDS,
            client_timeout=_CLIENT_TIMEOUT_SECONDS,
            concurrency="thread",
            worker_thread_name="egress-tunnel",
            raw_client=True,
            deactivate_after_join=True,
        )

    @classmethod
    def create(
        cls, layout: StateLayout, agent: str, policy: EgressPolicy
    ) -> "EgressBrokerRuntime":
        return cls(EgressBrokerSession.create(layout, agent, policy))

    def _reserve_tunnel(self) -> int:
        with self._limit_lock:
            pending_creations = len(
                self._active_reservations - self._created_reservations
            )
            if (
                len(self._active_reservations) >= _MAX_ACTIVE_TUNNELS
                or self._created_tunnels + pending_creations
                >= _MAX_CREATED_TUNNELS
            ):
                raise ValueError("egress tunnel limit reached")
            reservation = self._next_reservation
            self._next_reservation += 1
            self._active_reservations.add(reservation)
            return reservation

    def _mark_tunnel_created(self, reservation: int) -> None:
        with self._limit_lock:
            if (
                reservation not in self._active_reservations
                or reservation in self._created_reservations
                or self._created_tunnels >= _MAX_CREATED_TUNNELS
            ):
                raise ValueError("egress tunnel reservation is invalid")
            self._created_reservations.add(reservation)
            self._created_tunnels += 1

    def _release_tunnel(self, reservation: int) -> None:
        with self._limit_lock:
            if reservation not in self._active_reservations:
                raise ValueError("egress tunnel reservation is invalid")
            self._active_reservations.remove(reservation)
            self._created_reservations.discard(reservation)

    @property
    def _thread(self) -> Any | None:
        return self._runtime.thread

    def __enter__(self) -> EgressRuntimeMount:
        self._runtime.start()
        return EgressRuntimeMount(
            self.session.run_dir, self.session.project_id, self.session.agent
        )

    def wait_failed(self, timeout: float) -> bool:
        return self._runtime.wait_failed(timeout)

    def _write_response(
        self, stream: BinaryIO, status: str, code: str
    ) -> None:
        stream.write(encode_response_frame(EgressResponse(1, status, code)))
        stream.flush()

    def _handle_client(self, client: socket.socket) -> None:
        connection = open_connection(client, timeout=_CLIENT_TIMEOUT_SECONDS)
        stream: BinaryIO = connection.stream
        peer_uid = connection.peer_uid
        reservation: int | None = None
        upstream: socket.socket | None = None
        try:
            try:
                request = read_request_frame(stream)
                domain = self.session.authorize(request, peer_uid)
            except (OSError, ValueError) as error:
                stage = (
                    "policy"
                    if getattr(error, "stage", None) == "policy"
                    else "authentication"
                )
                self._write_response(stream, "denied", stage)
                self.session.audit("denied", stage=stage)
                return
            try:
                reservation = self._reserve_tunnel()
            except ValueError:
                self._write_response(stream, "denied", "limit")
                self.session.audit("denied", stage="limit")
                return
            try:
                targets = resolve_target(domain)
            except ValueError:
                self._write_response(stream, "denied", "resolve")
                self.session.audit("denied", stage="resolve")
                return
            try:
                upstream = connect_target(targets[0])
            except ValueError:
                self._write_response(stream, "error", "connect")
                self.session.audit("error", stage="connect")
                return
            self._mark_tunnel_created(reservation)
            self._write_response(stream, "ok", "connect")
            try:
                counts = relay_tunnel(client, upstream, RelayLimits())
            except ValueError:
                self.session.audit("error", stage="relay")
                return
            self.session.audit(
                "ok",
                bytes_from_client=counts.from_client,
                bytes_from_upstream=counts.from_upstream,
            )
        finally:
            if upstream is not None:
                upstream.close()
            if reservation is not None:
                self._release_tunnel(reservation)
            stream.close()

    def __exit__(self, *_: object) -> None:
        self._runtime.stop(join_timeout=_STOP_TIMEOUT_SECONDS)
