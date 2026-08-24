import json
from pathlib import Path
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


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def validate_managed_policy(settings_path: Path, mcp_path: Path) -> bool:
    return (
        _load_json(settings_path) == EXPECTED_SETTINGS
        and _load_json(mcp_path) == EXPECTED_MCP
    )


def main(
    settings_path: Path = Path("/etc/claude-code/managed-settings.json"),
    mcp_path: Path = Path("/etc/claude-code/managed-mcp.json"),
    stdout: TextIO = sys.stdout,
) -> int:
    valid = validate_managed_policy(settings_path, mcp_path)
    stdout.write(f"managed_policy_valid={'true' if valid else 'false'}\n")
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
