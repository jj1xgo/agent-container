import json
import os
from pathlib import Path
import stat
import sys
from typing import TextIO


EXPECTED_SETTINGS = {
    "sandbox": {
        "enabled": True,
        "enableWeakerNestedSandbox": True,
        "allowUnsandboxedCommands": False,
        "failIfUnavailable": True,
        "credentials": {
            "envVars": [
                {"name": "CLAUDE_CODE_OAUTH_TOKEN", "mode": "deny"},
                {"name": "ANTHROPIC_API_KEY", "mode": "deny"},
                {"name": "ANTHROPIC_AUTH_TOKEN", "mode": "deny"},
                {"name": "AWS_ACCESS_KEY_ID", "mode": "deny"},
                {"name": "AWS_SECRET_ACCESS_KEY", "mode": "deny"},
                {"name": "AWS_SESSION_TOKEN", "mode": "deny"},
                {"name": "GOOGLE_APPLICATION_CREDENTIALS", "mode": "deny"},
                {"name": "GOOGLE_API_KEY", "mode": "deny"},
                {"name": "AZURE_CLIENT_SECRET", "mode": "deny"},
            ],
            "files": [
                {"path": "/run/secrets/claude-oauth-token", "mode": "deny"}
            ],
        },
    },
    "permissions": {
        "deny": ["Read(//run/secrets/claude-oauth-token)"],
        "disableBypassPermissionsMode": "disable",
    },
    "disableAllHooks": True,
    "allowManagedHooksOnly": True,
    "allowedMcpServers": [],
    "allowManagedMcpServersOnly": True,
}
EXPECTED_MCP = {"mcpServers": {}}
_MAX_POLICY_BYTES = 64 * 1024


def _open_policy_directory(path: Path, expected_uid: int | None) -> int | None:
    if not path.is_absolute() or any(part in (".", "..") for part in path.parts):
        return None
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open("/", flags)
    except OSError:
        return None
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (
                expected_uid is not None
                and (
                    metadata.st_uid != expected_uid
                    or metadata.st_mode & 0o022
                )
            )
        ):
            os.close(descriptor)
            return None
        return descriptor
    except OSError:
        os.close(descriptor)
        return None


def _load_json(path: Path, expected_uid: int | None) -> object:
    flags = (
        os.O_RDONLY
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directory_descriptor = _open_policy_directory(path.parent, expected_uid)
    if directory_descriptor is None:
        return None
    try:
        descriptor = os.open(path.name, flags, dir_fd=directory_descriptor)
    except OSError:
        os.close(directory_descriptor)
        return None
    os.close(directory_descriptor)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _MAX_POLICY_BYTES
            or (
                expected_uid is not None
                and (
                    metadata.st_uid != expected_uid
                    or metadata.st_mode & 0o022
                )
            )
        ):
            return None
        payload = bytearray()
        while len(payload) <= _MAX_POLICY_BYTES:
            chunk = os.read(
                descriptor,
                min(4096, _MAX_POLICY_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > _MAX_POLICY_BYTES:
            return None
        return json.loads(bytes(payload).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    finally:
        os.close(descriptor)


def validate_managed_policy(
    settings_path: Path,
    mcp_path: Path,
    *,
    expected_uid: int | None = None,
) -> bool:
    return (
        _load_json(settings_path, expected_uid) == EXPECTED_SETTINGS
        and _load_json(mcp_path, expected_uid) == EXPECTED_MCP
    )


def main(
    settings_path: Path = Path("/etc/claude-code/managed-settings.json"),
    mcp_path: Path = Path("/etc/claude-code/managed-mcp.json"),
    stdout: TextIO = sys.stdout,
) -> int:
    valid = validate_managed_policy(settings_path, mcp_path, expected_uid=0)
    stdout.write(f"managed_policy_valid={'true' if valid else 'false'}\n")
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
