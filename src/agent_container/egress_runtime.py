import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import time
from typing import Callable
from typing import Iterable
from typing import Protocol
from typing import Sequence


ADAPTER_EXECUTABLE = Path("/usr/local/bin/agent-egress-adapter")
RUNTIME_EXECUTABLE = Path("/usr/local/bin/agent-egress-runtime")
_READINESS_TIMEOUT_SECONDS = 5


class ChildProcess(Protocol):
    returncode: int | None

    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def send_signal(self, signum: int) -> None: ...


def validate_agent_command(command: Sequence[str]) -> list[str]:
    validated = list(command)
    if not validated:
        raise ValueError("egress runtime agent command is invalid")
    if validated[0] == "codex":
        return validated
    if validated[:3] == ["python3", "-m", "agent_container.claude_launcher"]:
        return validated
    raise ValueError("egress runtime agent command is invalid")


def _managed_executable(path: Path) -> bool:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not path.is_symlink()
        and stat.S_IMODE(metadata.st_mode) & 0o111 != 0
        and os.access(path, os.X_OK)
    )


def self_check(
    adapter_path: Path = ADAPTER_EXECUTABLE,
    runtime_path: Path = RUNTIME_EXECUTABLE,
) -> int:
    if not all(_managed_executable(path) for path in (adapter_path, runtime_path)):
        return 1
    try:
        checked = subprocess.run(
            [str(adapter_path), "--self-check"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_READINESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1
    return 0 if checked.returncode == 0 else 1


def forward_signal(processes: Iterable[ChildProcess], signum: int) -> None:
    for process in processes:
        if process.poll() is None:
            try:
                process.send_signal(signum)
            except OSError:
                pass


def _terminate(process: ChildProcess | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


def run(
    argv: Sequence[str],
    *,
    launcher: Callable[..., ChildProcess] = subprocess.Popen,
    poll_interval: float = 0.05,
) -> int:
    if list(argv) == ["--self-check"]:
        return self_check()
    if not argv or argv[0] != "--":
        return 2
    try:
        command = validate_agent_command(argv[1:])
    except ValueError:
        return 2

    ready_read, ready_write = os.pipe()
    adapter: ChildProcess | None = None
    child: ChildProcess | None = None
    active_processes: list[ChildProcess] = []
    received_signals: list[int] = []
    previous_handlers: dict[int, object] = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(
                signum,
                lambda received, _frame, active=active_processes, seen=received_signals: (
                    seen.append(received), forward_signal(active, received)
                ),
            )
        adapter = launcher(
            [str(ADAPTER_EXECUTABLE), "--ready-fd", str(ready_write)],
            pass_fds=(ready_write,),
            close_fds=True,
        )
        active_processes.append(adapter)
        os.close(ready_write)
        ready_write = -1
        readable, _, _ = select.select(
            [ready_read], [], [], _READINESS_TIMEOUT_SECONDS
        )
        if not readable or os.read(ready_read, 6) != b"ready\n" or adapter.poll() is not None:
            return 1
        if received_signals:
            return 128 + received_signals[0]
        child = launcher(command, close_fds=True)
        active_processes.append(child)
        if received_signals:
            return 128 + received_signals[0]
        while True:
            child_status = child.poll()
            if child_status is not None:
                _terminate(adapter)
                return child_status
            if adapter.poll() is not None:
                _terminate(child)
                return 1
            time.sleep(poll_interval)
    except (OSError, ValueError):
        return 1
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        _terminate(child)
        _terminate(adapter)
        os.close(ready_read)
        if ready_write >= 0:
            os.close(ready_write)


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
