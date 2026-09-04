"""Host-side broker runtime: private artifacts and the serve lifecycle."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import secrets
import socket
import stat
import struct
import threading
from typing import Any
from typing import Callable

from agent_container.broker.capability import CAPABILITY_PATTERN
from agent_container.broker.readiness import AlwaysReady
from agent_container.broker.readiness import ReadinessGate


MAX_UNIX_SOCKET_PATH_BYTES = 107
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def create_private_file(path: Path, body: str, *, label: str) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
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


@dataclass
class SocketBrokerRuntime:
    label: str
    thread_name: str
    open_listener: Callable[[int], Any]
    handler: Callable[[Any, int], object]
    deactivate: Callable[[], None]
    close: Callable[[], None]
    error_type: type[Exception]
    readiness: ReadinessGate = field(default_factory=AlwaysReady)
    backlog: int = 4
    listener_timeout: float = 0.2
    client_timeout: float = 30
    stop_event: threading.Event = field(default_factory=threading.Event, init=False)
    thread: Any | None = field(default=None, init=False)
    listener: Any | None = field(default=None, init=False, repr=False)
    error: BaseException | None = field(default=None, init=False, repr=False)
    exited: bool = field(default=False, init=False, repr=False)

    def start(self) -> None:
        if self.thread is not None or self.exited:
            raise self.error_type(f"{self.label} failed to start")
        listener: Any | None = None
        try:
            listener = self.open_listener(self.backlog)
            listener.settimeout(self.listener_timeout)
            self.listener = listener
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
            if not self.readiness.wait():
                raise self.error_type(f"{self.label} readiness gate failed")
            while not self.stop_event.is_set():
                try:
                    client, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self.stop_event.is_set():
                        break
                    raise
                with client:
                    client.settimeout(self.client_timeout)
                    credentials = client.getsockopt(
                        socket.SOL_SOCKET,
                        socket.SO_PEERCRED,
                        _PEER_CREDENTIAL_BYTES,
                    )
                    _pid, peer_uid, _gid = struct.unpack("3i", credentials)
                    stream = client.makefile("rwb", buffering=0)
                    try:
                        self.handler(stream, peer_uid)
                    finally:
                        stream.close()
        except BaseException as error:
            self.error = error

    def stop(self, *, join_timeout: float) -> None:
        if self.exited:
            return
        self.stop_event.set()
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
