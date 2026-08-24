from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Mapping
from typing import TextIO


TOKEN_NAME = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_MARKER = b"CLAUDE_CODE_OAUTH_TOKEN="
_MAX_ENVIRON_BYTES = 64 * 1024


@dataclass(frozen=True)
class ProbeResult:
    oauth_token_visible: bool
    token_file_readable: bool
    parent_token_via_proc_readable: bool


def _open_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _token_file_readable(path: Path) -> bool:
    try:
        descriptor = os.open(path, _open_flags())
    except OSError:
        return False
    os.close(descriptor)
    return True


def _environ_contains_token(path: Path) -> bool:
    try:
        descriptor = os.open(path, _open_flags())
    except OSError:
        return False
    try:
        remaining = _MAX_ENVIRON_BYTES
        tail = b""
        while remaining:
            try:
                chunk = os.read(descriptor, min(4096, remaining))
            except OSError:
                return False
            if not chunk:
                return False
            window = tail + chunk
            if TOKEN_MARKER in window:
                return True
            tail = window[-(len(TOKEN_MARKER) - 1) :]
            remaining -= len(chunk)
        return False
    finally:
        os.close(descriptor)


def _parent_token_via_proc_readable(proc_root: Path) -> bool:
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return False
    own_pid = str(os.getpid())
    for entry in entries:
        if not entry.name.isascii() or not entry.name.isdecimal():
            continue
        if entry.name == own_pid:
            continue
        if _environ_contains_token(entry / "environ"):
            return True
    return False


def run_probe(
    token_path: Path,
    proc_root: Path,
    environment: Mapping[str, str],
) -> ProbeResult:
    return ProbeResult(
        oauth_token_visible=TOKEN_NAME in environment,
        token_file_readable=_token_file_readable(token_path),
        parent_token_via_proc_readable=_parent_token_via_proc_readable(proc_root),
    )


def _boolean(value: bool) -> str:
    return "true" if value else "false"


def render(result: ProbeResult) -> str:
    return (
        f"oauth_token_visible={_boolean(result.oauth_token_visible)}\n"
        f"token_file_readable={_boolean(result.token_file_readable)}\n"
        "parent_token_via_proc_readable="
        f"{_boolean(result.parent_token_via_proc_readable)}\n"
    )


def main(
    token_path: Path = Path("/run/secrets/claude-oauth-token"),
    proc_root: Path = Path("/proc"),
    environment: Mapping[str, str] | None = None,
    stdout: TextIO = sys.stdout,
) -> int:
    result = run_probe(
        token_path,
        proc_root,
        os.environ if environment is None else environment,
    )
    stdout.write(render(result))
    return 0 if result == ProbeResult(False, False, False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
