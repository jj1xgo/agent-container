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
from typing import Callable

from agent_container.broker.artifacts import RuntimeArtifacts
from agent_container.broker.runtime import accept_clients
from agent_container.family_intake_broker import FamilyIntakeSession
from agent_container.family_intake_transport import handle_family_intake_connection
from agent_container.family_pending import initialize_pending_store
from agent_container.family_state import FamilyStateLayout
from agent_container.family_state import load_family_binding
from agent_container.family_runtime_mount import FamilyRuntimeMount
from agent_container.family_runtime_mount import FamilyRuntimeError
from agent_container.state import ensure_private_directory
from agent_container.state import validate_agent
from agent_container.state import validate_project_id


_CAPABILITY = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CAPABILITY_TTL_SECONDS = 24 * 60 * 60
_CLIENT_TIMEOUT_SECONDS = 30
_LISTENER_BACKLOG = 8
_LISTENER_TIMEOUT_SECONDS = 0.2
_MAX_UNIX_SOCKET_PATH_BYTES = 107
_RUN_ID_ATTEMPTS = 16
_STOP_TIMEOUT_SECONDS = 2
_CONTAINER_SOCKET = "/run/agent-family/intake.sock"
_LABEL = "family intake"


class FamilyIntakeRuntimeError(FamilyRuntimeError):
    pass


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


@dataclass
class FamilyIntakeRuntime(AbstractContextManager[FamilyRuntimeMount]):
    layout: FamilyStateLayout
    clock: Callable[[], int] = field(default=lambda: int(time.time()), repr=False)
    random_bytes: Callable[[int], bytes] = field(default=os.urandom, repr=False)
    agent: str = field(kw_only=True)
    repository: str = field(kw_only=True)
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
    _artifacts: RuntimeArtifacts | None = field(default=None, init=False, repr=False)
    _cleanup_complete: bool = field(default=False, init=False, repr=False)

    @classmethod
    def create(
        cls,
        layout: FamilyStateLayout,
        *,
        clock: Callable[[], int] = lambda: int(time.time()),
        random_bytes: Callable[[int], bytes] = os.urandom,
        agent: str,
        repository: str,
    ) -> "FamilyIntakeRuntime":
        if type(layout) is not FamilyStateLayout:
            raise ValueError("family intake runtime is invalid")
        if not callable(clock) or not callable(random_bytes):
            raise ValueError("family intake runtime is invalid")
        try:
            agent = validate_agent(agent)
            repository = validate_project_id(repository)
        except (TypeError, ValueError):
            raise ValueError("family intake runtime is invalid") from None
        return cls(
            layout=layout,
            clock=clock,
            random_bytes=random_bytes,
            agent=agent,
            repository=repository,
        )

    def _create_run_directory(self) -> Path:
        ensure_private_directory(self.layout.root)
        for directory in (
            self.layout.family_root,
            self.layout.family_root / "intake",
            self.layout.family_root / "intake" / "r",
            self.layout.family_intake_run_root,
        ):
            ensure_private_directory(directory, create=True)
        for _attempt in range(_RUN_ID_ATTEMPTS):
            generated = self.random_bytes(8)
            if type(generated) is not bytes or len(generated) != 8:
                raise ValueError("family intake random source is invalid")
            run_dir = self.layout.family_intake_run_root / generated.hex()
            try:
                run_dir.mkdir(mode=0o700)
            except FileExistsError:
                continue
            try:
                # Opens the parent and the run directory by descriptor, requires
                # mode 0700 and the current uid, and captures the identity that
                # cleanup compares against (K2).
                self._artifacts = RuntimeArtifacts.open(run_dir, label=_LABEL)
            except BaseException:
                try:
                    os.rmdir(run_dir)
                except OSError:
                    pass
                raise
            return run_dir
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
            for directory in (
                self.layout.family_pending_dir,
                self.layout.family_audit_file.parent,
            ):
                ensure_private_directory(directory, create=True)
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
                agent=self.agent,
                repository=self.repository,
                owner_uid=os.getuid(),
                clock=self.clock,
                random_bytes=self.random_bytes,
            )
            socket_path = run_dir / "intake.sock"
            if len(os.fsencode(socket_path)) > _MAX_UNIX_SOCKET_PATH_BYTES:
                raise ValueError("family intake socket path is too long")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            artifacts = self._artifacts
            if artifacts is None:
                raise ValueError("family intake run directory is unavailable")
            # Capture the bound socket's identity before chmod: cleanup only
            # unlinks this inode (F2).
            artifacts.track_socket("intake.sock")
            dir_fd = artifacts.dir_fd
            socket_stat = os.stat("intake.sock", dir_fd=dir_fd, follow_symlinks=False)
            os.chmod("intake.sock", 0o600, dir_fd=dir_fd, follow_symlinks=False)
            secured = os.stat("intake.sock", dir_fd=dir_fd, follow_symlinks=False)
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
            self._mount = FamilyRuntimeMount.capture(
                run_dir, capability, environment
            )
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
            if self._mount is not None:
                try:
                    self._mount.close()
                except OSError:
                    pass
                self._mount = None
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

    def validate_mount(self) -> None:
        if self._mount is None or self._stop.is_set():
            raise FamilyIntakeRuntimeError(
                "family intake runtime mount is invalid"
            )
        try:
            self._mount.revalidate()
        except ValueError:
            raise FamilyIntakeRuntimeError(
                "family intake runtime mount is invalid"
            ) from None

    def _serve(self, listener: socket.socket) -> None:
        try:
            for client in accept_clients(listener, stop_event=self._stop):
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
        artifacts = self._artifacts
        self._artifacts = None
        self._cleanup_complete = True
        if artifacts is None:
            return
        try:
            cleanup_failed = artifacts.remove()
        finally:
            # Family closes both descriptors even when removal failed; its
            # close() is not retried (unchanged behavior).
            artifacts.close()
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
        mount = self._mount
        try:
            self._cleanup_artifacts()
        except (OSError, ValueError):
            cleanup_failed = True
        if mount is not None:
            try:
                mount.close()
            except OSError:
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
