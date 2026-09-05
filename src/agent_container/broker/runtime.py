"""Host-side broker runtime: private artifacts and the serve lifecycle."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import secrets
import socket
import stat
import struct
import threading
import time
from typing import Any
from typing import Callable
from typing import Iterator

from agent_container.broker.capability import CAPABILITY_PATTERN
from agent_container.broker.readiness import AlwaysReady
from agent_container.broker.readiness import ReadinessGate


MAX_UNIX_SOCKET_PATH_BYTES = 107
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def create_private_file(
    path: Path, body: str, *, label: str, mode: int = 0o600
) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        encoded = body.encode("ascii")
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError(f"{label} private file write failed")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def allocate_run_dir(
    project_root: Path, *, label: str, attempts: int = 8
) -> tuple[str, Path]:
    for _ in range(attempts):
        run_id = secrets.token_hex(8)
        run_dir = project_root / run_id
        try:
            run_dir.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return run_id, run_dir
    raise FileExistsError(f"could not allocate {label} runtime")


def generate_capability(*, label: str) -> str:
    capability = secrets.token_urlsafe(32)
    if CAPABILITY_PATTERN.fullmatch(capability) is None:
        raise RuntimeError(f"generated {label} capability has invalid format")
    return capability


def bind_private_listener(
    socket_path: Path, *, backlog: int, label: str
) -> socket.socket:
    if len(os.fsencode(socket_path)) > MAX_UNIX_SOCKET_PATH_BYTES:
        raise ValueError(f"{label} socket path is too long")
    if socket_path.exists() or socket_path.is_symlink():
        raise FileExistsError(f"{label} socket path already exists")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        listener.listen(backlog)
    except Exception:
        listener.close()
        try:
            metadata = socket_path.lstat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISSOCK(metadata.st_mode):
                socket_path.unlink()
        raise
    return listener


def remove_runtime_artifacts(
    *, capability_path: Path, socket_path: Path, run_dir: Path
) -> bool:
    failed = False
    for path, expected_type in (
        (capability_path, stat.S_ISREG),
        (socket_path, stat.S_ISSOCK),
    ):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            failed = True
            continue
        if not expected_type(metadata.st_mode):
            failed = True
            continue
        try:
            path.unlink()
        except OSError:
            failed = True
    try:
        run_dir.rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        failed = True
    return failed


_PEER_CREDENTIAL_BYTES = 12
_CONCURRENCY_MODES = frozenset({"inline", "thread"})


@dataclass(frozen=True)
class Connection:
    client: Any
    stream: Any
    peer_uid: int


def open_connection(client: Any, *, timeout: float) -> Connection:
    client.settimeout(timeout)
    credentials = client.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        _PEER_CREDENTIAL_BYTES,
    )
    _pid, peer_uid, _gid = struct.unpack("3i", credentials)
    return Connection(client, client.makefile("rwb", buffering=0), peer_uid)


def accept_clients(listener: Any, *, stop_event: threading.Event) -> Iterator[Any]:
    while not stop_event.is_set():
        try:
            client, _ = listener.accept()
        except TimeoutError:
            continue
        except OSError:
            if stop_event.is_set():
                return
            raise
        yield client


@dataclass
class SocketBrokerRuntime:
    label: str
    thread_name: str
    open_listener: Callable[[int], Any]
    # raw_client=False: handler(Connection). raw_client=True: handler(client socket).
    handler: Callable[[Any], object]
    deactivate: Callable[[], None]
    close: Callable[[], None]
    error_type: type[Exception]
    readiness: ReadinessGate = field(default_factory=AlwaysReady)
    backlog: int = 4
    listener_timeout: float = 0.2
    client_timeout: float = 30
    concurrency: str = "inline"
    worker_thread_name: str = ""
    raw_client: bool = False
    deactivate_after_join: bool = False
    stop_event: threading.Event = field(default_factory=threading.Event, init=False)
    failed: threading.Event = field(default_factory=threading.Event, init=False)
    thread: Any | None = field(default=None, init=False)
    listener: Any | None = field(default=None, init=False, repr=False)
    error: BaseException | None = field(default=None, init=False, repr=False)
    exited: bool = field(default=False, init=False, repr=False)
    workers: set[Any] = field(default_factory=set, init=False, repr=False)
    worker_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.concurrency not in _CONCURRENCY_MODES:
            raise ValueError(f"{self.label} concurrency mode is invalid")

    def start(self) -> None:
        if self.thread is not None or self.exited:
            raise self.error_type(f"{self.label} failed to start")
        listener: Any | None = None
        try:
            listener = self.open_listener(self.backlog)
            listener.settimeout(self.listener_timeout)
            self.listener = listener
            # Keep this as attribute access on the threading module: the
            # handover runtime tests patch threading.Thread globally and rely
            # on the kernel picking the patched class up.
            thread = threading.Thread(
                target=self._serve,
                args=(listener,),
                name=self.thread_name,
                daemon=True,
            )
            thread.start()
            self.thread = thread
        except BaseException:
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass
            cleanup_complete = False
            try:
                self.close()
            except (OSError, ValueError):
                pass
            else:
                cleanup_complete = True
            self.exited = cleanup_complete
            raise self.error_type(f"{self.label} failed to start") from None

    def _serve(self, listener: Any) -> None:
        try:
            while not self.stop_event.is_set():
                if self.readiness.wait(self.listener_timeout):
                    break
            else:
                return
            for client in accept_clients(listener, stop_event=self.stop_event):
                if self.concurrency == "thread":
                    self._start_worker(client)
                else:
                    with client:
                        self._handle_client(client)
        except BaseException as error:
            self.error = error
            self.failed.set()

    def _handle_client(self, client: Any) -> None:
        if self.raw_client:
            self.handler(client)
            return
        connection = open_connection(client, timeout=self.client_timeout)
        try:
            self.handler(connection)
        finally:
            connection.stream.close()

    def _start_worker(self, client: Any) -> None:
        thread = threading.Thread(
            target=self._run_worker,
            args=(client,),
            name=self.worker_thread_name or f"{self.thread_name}-worker",
            daemon=True,
        )
        with self.worker_lock:
            self.workers.add(thread)
        try:
            thread.start()
        except BaseException:
            with self.worker_lock:
                self.workers.discard(thread)
            client.close()
            raise

    def _run_worker(self, client: Any) -> None:
        try:
            self._handle_client(client)
        except OSError:
            pass
        except BaseException as error:
            self.error = error
            self.stop_event.set()
            self.failed.set()
        finally:
            client.close()
            with self.worker_lock:
                self.workers.discard(threading.current_thread())

    def wait_failed(self, timeout: float) -> bool:
        return self.failed.wait(timeout)

    def _join_workers(self, join_timeout: float) -> bool:
        deadline = time.monotonic() + join_timeout
        with self.worker_lock:
            workers = tuple(self.workers)
        for worker in workers:
            # Registration precedes start(), which the accept thread may still
            # be entering after its join times out. Keep pending workers tracked
            # for the retry instead of joining a thread that has not started.
            if worker.is_alive():
                worker.join(timeout=max(0, deadline - time.monotonic()))
        with self.worker_lock:
            return bool(self.workers)

    def stop(self, *, join_timeout: float) -> None:
        if self.exited:
            return
        self.stop_event.set()
        if not self.deactivate_after_join:
            self.deactivate()
        cleanup_failed = False
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                cleanup_failed = True
            else:
                self.listener = None

        did_not_stop = False
        if self.thread is not None:
            self.thread.join(timeout=join_timeout)
            did_not_stop = self.thread.is_alive()
        if self._join_workers(join_timeout):
            did_not_stop = True
        if self.deactivate_after_join:
            self.deactivate()

        if did_not_stop:
            raise self.error_type(f"{self.label} did not stop") from None

        try:
            self.close()
        except (OSError, ValueError):
            cleanup_failed = True
        else:
            self.exited = True

        if cleanup_failed:
            raise self.error_type(f"{self.label} cleanup failed") from None
        if self.error is not None:
            raise self.error_type(f"{self.label} failed") from None
