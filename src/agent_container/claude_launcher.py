import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Callable
from typing import NoReturn
from typing import Sequence

from agent_container.state import validate_claude_oauth_token

_CONFIG_NAME = ".claude.json"
_TRUST_KEY = "hasTrustDialogAccepted"
_CONFIG_LIMIT = 16 * 1024 * 1024


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
        chunks = bytearray()
        while len(chunks) < 4097:
            chunk = os.read(descriptor, 4097 - len(chunks))
            if not chunk:
                break
            chunks.extend(chunk)
    finally:
        os.close(descriptor)
    try:
        token = bytes(chunks).decode("ascii", errors="strict")
    except UnicodeDecodeError:
        raise ValueError("Claude OAuth token must be ASCII") from None
    return validate_claude_oauth_token(token)


def _read_config(path: Path) -> dict | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("Claude config must be a regular file")
        if details.st_uid != os.getuid():
            raise PermissionError("Claude config must be owned by the current user")
        if details.st_size > _CONFIG_LIMIT:
            raise ValueError("Claude config is larger than the launcher limit")
        payload = bytearray()
        while len(payload) <= _CONFIG_LIMIT:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            payload.extend(chunk)
    finally:
        os.close(descriptor)
    try:
        config = json.loads(bytes(payload))
    except ValueError:
        raise ValueError("Claude config must be valid JSON") from None
    if not isinstance(config, dict):
        raise ValueError("Claude config must be a JSON object")
    return config


def _write_config(path: Path, config: dict) -> None:
    payload = json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8")
    descriptor, staged = tempfile.mkstemp(prefix=_CONFIG_NAME + ".", dir=path.parent)
    try:
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(staged, path)
    except BaseException:
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise


def seed_workspace_trust(config_dir: Path, workspace: Path) -> None:
    path = config_dir / _CONFIG_NAME
    project = str(workspace)
    config = _read_config(path)
    if config is None:
        config = {}
    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        raise ValueError("Claude config projects must be a JSON object")
    entry = projects.get(project, {})
    if not isinstance(entry, dict):
        raise ValueError("Claude config project entry must be a JSON object")
    if entry.get(_TRUST_KEY) is True:
        return
    updated = {
        **config,
        "projects": {**projects, project: {**entry, _TRUST_KEY: True}},
    }
    _write_config(path, updated)


def exec_claude(
    token_path: Path,
    arguments: Sequence[str],
    execvpe: Callable[[str, tuple[str, ...], dict[str, str]], NoReturn] = os.execvpe,
) -> NoReturn:
    argv = tuple(arguments)
    if not argv:
        raise ValueError("Claude command arguments must not be empty")
    token = load_token(token_path)
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if not config_dir:
        raise ValueError("CLAUDE_CONFIG_DIR must be set before launching Claude")
    seed_workspace_trust(Path(config_dir), Path(os.path.realpath(os.getcwd())))
    environment = os.environ.copy()
    environment.pop("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", None)
    environment["IS_DEMO"] = "1"
    environment["CLAUDE_CODE_OAUTH_TOKEN"] = token
    execvpe(argv[0], argv, environment)


def main(arguments: Sequence[str] | None = None) -> NoReturn:
    argv = tuple(sys.argv[1:] if arguments is None else arguments)
    if len(argv) < 3 or argv[1] != "--" or argv[2] != "claude":
        raise ValueError("usage: claude_launcher TOKEN_PATH -- claude [ARG ...]")
    exec_claude(Path(argv[0]), argv[2:])


if __name__ == "__main__":
    main()
