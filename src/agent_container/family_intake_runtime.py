"""Host-owned lifecycle for the credential-free family intake socket."""

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
import base64
import os
from pathlib import Path
import re
import socket
import stat
import threading
import time
from types import MappingProxyType
from typing import Callable, Mapping

from agent_container.family_intake_broker import FamilyIntakeSession
from agent_container.family_intake_transport import handle_family_intake_connection
from agent_container.family_pending import initialize_pending_store
from agent_container.family_state import FamilyStateLayout
from agent_container.family_state import load_family_binding
from agent_container.state import ensure_private_directory


_CAPABILITY = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CAPABILITY_TTL_SECONDS = 24 * 60 * 60
_CLIENT_TIMEOUT_SECONDS = 30
_LISTENER_BACKLOG = 8
_LISTENER_TIMEOUT_SECONDS = 0.2
_MAX_UNIX_SOCKET_PATH_BYTES = 107
_RUN_ID_ATTEMPTS = 16
_STOP_TIMEOUT_SECONDS = 2
_CONTAINER_SOCKET = "/run/agent-family/intake.sock"
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class FamilyIntakeRuntimeError(Exception):
    pass


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _private_directory(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) == 0o700
        and metadata.st_uid == os.getuid()
    )


@dataclass(frozen=True)
class FamilyRuntimeMount:
    socket_dir: Path
    capability: str = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.socket_dir, Path) or not self.socket_dir.is_absolute():
            raise ValueError("family runtime mount is invalid")
        if type(self.capability) is not str or _CAPABILITY.fullmatch(self.capability) is None:
            raise ValueError("family runtime mount is invalid")
        expected = {
            "AGENT_FAMILY_SOCKET": _CONTAINER_SOCKET,
            "AGENT_FAMILY_CAPABILITY": self.capability,
        }
        if dict(self.environment) != expected:
            raise ValueError("family runtime mount is invalid")
        object.__setattr__(self, "environment", MappingProxyType(expected))

    @property
    def socket_path(self) -> Path:
        return self.socket_dir / "intake.sock"


@dataclass
class FamilyIntakeRuntime(AbstractContextManager[FamilyRuntimeMount]):
    layout: FamilyStateLayout
    clock: Callable[[], int] = field(default=lambda: int(time.time()), repr=False)
    random_bytes: Callable[[int], bytes] = field(default=os.urandom, repr=False)
    session: FamilyIntakeSession | None = field(default=None, init=False)
    _listener: socket.socket | None = field(default=None, init=False, repr=False)
    _client: socket.socket | None = field(default=None, init=False, repr=False)
    _client_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _error: bool = field(default=False, init=False, repr=False)
    _failure_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _mount: FamilyRuntimeMount | None = field(default=None, init=False, repr=False)
    _run_parent_descriptor: int | None = field(default=None, init=False, repr=False)
    _run_descriptor: int | None = field(default=None, init=False, repr=False)
    _run_id: str | None = field(default=None, init=False, repr=False)
    _run_stat: os.stat_result | None = field(default=None, init=False, repr=False)
    _socket_stat: os.stat_result | None = field(default=None, init=False, repr=False)
    _cleanup_complete: bool = field(default=False, init=False, repr=False)

    @classmethod
    def create(
        cls,
        layout: FamilyStateLayout,
        *,
        clock: Callable[[], int] = lambda: int(time.time()),
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> "FamilyIntakeRuntime":
        if type(layout) is not FamilyStateLayout:
            raise ValueError("family intake runtime is invalid")
        if not callable(clock) or not callable(random_bytes):
            raise ValueError("family intake runtime is invalid")
        return cls(layout, clock, random_bytes)

    def _create_run_directory(self) -> Path:
        ensure_private_directory(self.layout.root)
        for directory in (
            self.layout.family_root,
            self.layout.family_root / "intake",
            self.layout.family_root / "intake" / "r",
            self.layout.family_intake_run_root,
        ):
            ensure_private_directory(directory, create=True)
        parent = os.open(
            self.layout.family_intake_run_root,
            os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
        )
        self._run_parent_descriptor = parent
        for _attempt in range(_RUN_ID_ATTEMPTS):
            generated = self.random_bytes(8)
            if type(generated) is not bytes or len(generated) != 8:
                raise ValueError("family intake random source is invalid")
            run_id = generated.hex()
            try:
                os.mkdir(run_id, 0o700, dir_fd=parent)
            except FileExistsError:
                continue
            self._run_id = run_id
            run_stat = os.stat(run_id, dir_fd=parent, follow_symlinks=False)
            if not _private_directory(run_stat):
                raise PermissionError("family intake run directory is not private")
            self._run_stat = run_stat
            run_descriptor = os.open(
                run_id,
                os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
                dir_fd=parent,
            )
            opened = os.fstat(run_descriptor)
            if not _private_directory(opened) or not _same_inode(run_stat, opened):
                os.close(run_descriptor)
                raise PermissionError("family intake run directory is not private")
            self._run_descriptor = run_descriptor
            return self.layout.family_intake_run_root / run_id
        raise FileExistsError("could not allocate family intake runtime")

    def _new_capability(self) -> str:
        generated = self.random_bytes(32)
        if type(generated) is not bytes or len(generated) != 32:
            raise ValueError("family intake random source is invalid")
        capability = base64.urlsafe_b64encode(generated).rstrip(b"=").decode("ascii")
        if _CAPABILITY.fullmatch(capability) is None:
            raise ValueError("family intake random source is invalid")
        return capability

    def start(self) -> FamilyRuntimeMount:
        if self._mount is not None or self._cleanup_complete:
            raise FamilyIntakeRuntimeError("family intake runtime failed to start")
        listener: socket.socket | None = None
        try:
            load_family_binding(self.layout.family_binding_file)
            initialize_pending_store(
                self.layout.family_pending_dir,
                self.layout.project_id,
                audit_path=self.layout.family_audit_file,
                clock=self.clock,
            )
            observed_now = self.clock()
            if type(observed_now) is not int or observed_now < 0:
                raise ValueError("family intake clock is invalid")
            run_dir = self._create_run_directory()
            capability = self._new_capability()
            session = FamilyIntakeSession(
                self.layout.project_id,
                capability,
                observed_now + _CAPABILITY_TTL_SECONDS,
                store=self.layout.family_pending_dir,
                binding_path=self.layout.family_binding_file,
                audit_path=self.layout.family_audit_file,
                owner_uid=os.getuid(),
                clock=self.clock,
                random_bytes=self.random_bytes,
            )
            socket_path = run_dir / "intake.sock"
            if len(os.fsencode(socket_path)) > _MAX_UNIX_SOCKET_PATH_BYTES:
                raise ValueError("family intake socket path is too long")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            if self._run_descriptor is None:
                raise ValueError("family intake run directory is unavailable")
            socket_stat = os.stat(
                "intake.sock",
                dir_fd=self._run_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISSOCK(socket_stat.st_mode):
                raise ValueError("family intake socket is invalid")
            self._socket_stat = socket_stat
            os.chmod(
                "intake.sock",
                0o600,
                dir_fd=self._run_descriptor,
                follow_symlinks=False,
            )
            secured = os.stat(
                "intake.sock",
                dir_fd=self._run_descriptor,
                follow_symlinks=False,
            )
            if (
                not _same_inode(socket_stat, secured)
                or not stat.S_ISSOCK(secured.st_mode)
                or stat.S_IMODE(secured.st_mode) != 0o600
                or secured.st_uid != os.getuid()
            ):
                raise PermissionError("family intake socket is not private")
            listener.listen(_LISTENER_BACKLOG)
            listener.settimeout(_LISTENER_TIMEOUT_SECONDS)
            self.session = session
            self._listener = listener
            environment = {
                "AGENT_FAMILY_SOCKET": _CONTAINER_SOCKET,
                "AGENT_FAMILY_CAPABILITY": capability,
            }
            self._mount = FamilyRuntimeMount(run_dir, capability, environment)
            thread = threading.Thread(
                target=self._serve,
                args=(listener,),
                name="family-intake",
                daemon=True,
            )
            thread.start()
            self._thread = thread
            return self._mount
        except BaseException:
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass
            self._listener = None
            if self.session is not None:
                self.session.deactivate()
            try:
                self._cleanup_artifacts()
            except (OSError, ValueError):
                pass
            raise FamilyIntakeRuntimeError(
                "family intake runtime failed to start"
            ) from None

    def register_runtime(self, peer_pid: int) -> None:
        if self.session is None or self._mount is None or self._stop.is_set():
            raise FamilyIntakeRuntimeError(
                "family intake runtime registration failed"
            )
        try:
            self.session.register_runtime(peer_pid)
        except ValueError:
            raise FamilyIntakeRuntimeError(
                "family intake runtime registration failed"
            ) from None

    def _serve(self, listener: socket.socket) -> None:
        try:
            while not self._stop.is_set():
                try:
                    client, _address = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                with self._client_lock:
                    if self._stop.is_set():
                        client.close()
                        break
                    self._client = client
                try:
                    with client:
                        client.settimeout(_CLIENT_TIMEOUT_SECONDS)
                        if self.session is None:
                            raise ValueError("family intake session is unavailable")
                        handle_family_intake_connection(
                            client,
                            self.session,
                            self.layout.family_pending_dir,
                        )
                finally:
                    with self._client_lock:
                        if self._client is client:
                            self._client = None
                if self.session.failed:
                    self._fail_runtime()
                    break
                if self.session.consumed:
                    self._stop.set()
                    try:
                        listener.close()
                    except OSError:
                        pass
                    break
        except BaseException:
            if not self._stop.is_set():
                self._fail_runtime()

    def _interrupt_client(self) -> None:
        with self._client_lock:
            client = self._client
            if client is not None:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _fail_runtime(self) -> None:
        with self._failure_lock:
            self._error = True
            self._stop.set()
            if self._listener is not None:
                try:
                    self._listener.close()
                except OSError:
                    pass
            self._interrupt_client()

    def _cleanup_artifacts(self) -> None:
        cleanup_failed = False
        try:
            if self._run_descriptor is not None and self._socket_stat is not None:
                try:
                    current = os.stat(
                        "intake.sock",
                        dir_fd=self._run_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                except OSError:
                    cleanup_failed = True
                else:
                    if (
                        _same_inode(current, self._socket_stat)
                        and stat.S_ISSOCK(current.st_mode)
                    ):
                        try:
                            os.unlink("intake.sock", dir_fd=self._run_descriptor)
                        except OSError:
                            cleanup_failed = True
                    else:
                        cleanup_failed = True

            if (
                not cleanup_failed
                and self._run_parent_descriptor is not None
                and self._run_id is not None
                and self._run_stat is not None
            ):
                try:
                    current_run = os.stat(
                        self._run_id,
                        dir_fd=self._run_parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                except OSError:
                    cleanup_failed = True
                else:
                    if _same_inode(current_run, self._run_stat) and _private_directory(
                        current_run
                    ):
                        try:
                            os.rmdir(self._run_id, dir_fd=self._run_parent_descriptor)
                        except OSError:
                            cleanup_failed = True
                    else:
                        cleanup_failed = True
        finally:
            for attribute in ("_run_descriptor", "_run_parent_descriptor"):
                descriptor = getattr(self, attribute)
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        cleanup_failed = True
                    finally:
                        setattr(self, attribute, None)
            self._cleanup_complete = True
        if cleanup_failed:
            raise ValueError("family intake cleanup failed")

    def close(self) -> None:
        if self._cleanup_complete:
            return
        self._stop.set()
        if self.session is not None:
            self.session.deactivate()
        cleanup_failed = False
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                cleanup_failed = True
        self._interrupt_client()
        if self._thread is not None:
            self._thread.join(timeout=_STOP_TIMEOUT_SECONDS)
            if self._thread.is_alive():
                raise FamilyIntakeRuntimeError(
                    "family intake runtime did not stop"
                ) from None
        try:
            self._cleanup_artifacts()
        except (OSError, ValueError):
            cleanup_failed = True
        if cleanup_failed:
            raise FamilyIntakeRuntimeError(
                "family intake runtime cleanup failed"
            ) from None
        if self._error:
            raise FamilyIntakeRuntimeError("family intake runtime failed") from None

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def check(self) -> None:
        if self._error or (
            self._thread is not None
            and not self._thread.is_alive()
            and self.session is not None
            and not self.session.consumed
            and not self._stop.is_set()
        ):
            raise FamilyIntakeRuntimeError("family intake runtime failed")

    def __enter__(self) -> FamilyRuntimeMount:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.close()
