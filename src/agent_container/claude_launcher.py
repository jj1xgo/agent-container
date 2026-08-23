import os
from pathlib import Path
import stat
import sys
from typing import Callable
from typing import NoReturn
from typing import Sequence

from agent_container.state import validate_claude_oauth_token


def load_token(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("Claude OAuth token must be a regular file")
        if stat.S_IMODE(details.st_mode) != 0o600:
            raise PermissionError("Claude OAuth token must have mode 0600")
        if details.st_uid != os.getuid():
            raise PermissionError("Claude OAuth token must be owned by the current user")
        value = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    try:
        token = value.decode("ascii", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("Claude OAuth token must be ASCII") from error
    return validate_claude_oauth_token(token)


def exec_claude(
    token_path: Path,
    arguments: Sequence[str],
    execvpe: Callable[[str, tuple[str, ...], dict[str, str]], NoReturn] = os.execvpe,
) -> NoReturn:
    argv = tuple(arguments)
    if not argv:
        raise ValueError("Claude command arguments must not be empty")
    token = load_token(token_path)
    environment = os.environ.copy()
    environment["CLAUDE_CODE_OAUTH_TOKEN"] = token
    environment["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] = "1"
    execvpe(argv[0], argv, environment)


def main(arguments: Sequence[str] | None = None) -> NoReturn:
    argv = tuple(sys.argv[1:] if arguments is None else arguments)
    if len(argv) < 3 or argv[1] != "--" or argv[2] != "claude":
        raise ValueError("usage: claude_launcher TOKEN_PATH -- claude [ARG ...]")
    exec_claude(Path(argv[0]), argv[2:])


if __name__ == "__main__":
    main()
