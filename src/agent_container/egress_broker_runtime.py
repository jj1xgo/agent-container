from contextlib import AbstractContextManager
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import socket
import struct
import threading
import time
from typing import BinaryIO

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
_PEER_CREDENTIAL_BYTES = 12


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
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _listener: socket.socket | None = field(default=None, init=False, repr=False)
    _error: BaseException | None = field(default=None, init=False, repr=False)
    _failed: threading.Event = field(default_factory=threading.Event, init=False)
    _exited: bool = field(default=False, init=False, repr=False)
    _limit_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _next_reservation: int = field(default=1, init=False, repr=False)
    _active_reservations: set[int] = field(default_factory=set, init=False, repr=False)
    _created_reservations: set[int] = field(default_factory=set, init=False, repr=False)
    _created_tunnels: int = field(default=0, init=False, repr=False)
    _worker_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _workers: set[threading.Thread] = field(
        default_factory=set, init=False, repr=False
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

    def __enter__(self) -> EgressRuntimeMount:
        if self._thread is not None or self._exited:
            raise EgressBrokerRuntimeError("egress broker failed to start")
        listener: socket.socket | None = None
        try:
            listener = self.session.open_listener(backlog=_LISTENER_BACKLOG)
            listener.settimeout(_LISTENER_TIMEOUT_SECONDS)
            self._listener = listener
            thread = threading.Thread(
                target=self._serve,
                args=(listener,),
                name="egress-broker",
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
            raise EgressBrokerRuntimeError("egress broker failed to start") from None
        return EgressRuntimeMount(
            self.session.run_dir, self.session.project_id, self.session.agent
        )

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
                self._start_worker(client)
        except BaseException as error:
            self._error = error
            self._failed.set()

    def _start_worker(self, client: socket.socket) -> None:
        thread = threading.Thread(
            target=self._run_worker,
            args=(client,),
            name="egress-tunnel",
            daemon=True,
        )
        with self._worker_lock:
            self._workers.add(thread)
        try:
            thread.start()
        except BaseException:
            with self._worker_lock:
                self._workers.discard(thread)
            client.close()
            raise

    def _run_worker(self, client: socket.socket) -> None:
        try:
            self._handle_client(client)
        except OSError:
            pass
        except BaseException as error:
            self._error = error
            self._failed.set()
            self._stop.set()
        finally:
            client.close()
            with self._worker_lock:
                self._workers.discard(threading.current_thread())

    def wait_failed(self, timeout: float) -> bool:
        return self._failed.wait(timeout)

    def _write_response(
        self, stream: BinaryIO, status: str, code: str
    ) -> None:
        stream.write(encode_response_frame(EgressResponse(1, status, code)))
        stream.flush()

    def _handle_client(self, client: socket.socket) -> None:
        client.settimeout(_CLIENT_TIMEOUT_SECONDS)
        credentials = client.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            _PEER_CREDENTIAL_BYTES,
        )
        _pid, peer_uid, _gid = struct.unpack("3i", credentials)
        stream: BinaryIO = client.makefile("rwb", buffering=0)
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
        if self._exited:
            return
        self._stop.set()
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
        deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
        while True:
            with self._worker_lock:
                workers = tuple(self._workers)
            if not workers:
                break
            for worker in workers:
                if worker.is_alive():
                    worker.join(timeout=max(0, deadline - time.monotonic()))
            with self._worker_lock:
                did_not_stop = did_not_stop or bool(self._workers)
            break
        if did_not_stop:
            self.session.deactivate()
            raise EgressBrokerRuntimeError("egress broker did not stop") from None
        self.session.deactivate()
        try:
            self.session.close()
        except (OSError, ValueError):
            cleanup_failed = True
        else:
            self._exited = True
        if cleanup_failed:
            raise EgressBrokerRuntimeError("egress broker cleanup failed") from None
        if self._error is not None:
            raise EgressBrokerRuntimeError("egress broker failed") from None
