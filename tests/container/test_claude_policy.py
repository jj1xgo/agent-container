from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest

from agent_container.claude_policy import main
from agent_container.claude_policy import validate_managed_policy


ROOT = Path(__file__).resolve().parents[2]


class ClaudeManagedPolicyTest(unittest.TestCase):
    def _write_policy(self, root: Path, settings: dict, mcp: dict) -> tuple[Path, Path]:
        settings_path = root / "managed-settings.json"
        mcp_path = root / "managed-mcp.json"
        settings_path.write_text(json.dumps(settings), encoding="utf-8")
        mcp_path.write_text(json.dumps(mcp), encoding="utf-8")
        settings_path.chmod(0o600)
        mcp_path.chmod(0o600)
        return settings_path, mcp_path

    def test_accepts_repository_managed_policy(self) -> None:
        self.assertTrue(
            validate_managed_policy(
                ROOT / "profiles/claude/managed-settings.json",
                ROOT / "profiles/claude/managed-mcp.json",
            )
        )

    def test_rejects_every_security_critical_mutation_without_output(self) -> None:
        baseline = json.loads(
            (ROOT / "profiles/claude/managed-settings.json").read_text()
        )
        mcp = {"mcpServers": {}}
        mutations = {
            "sandbox disabled": lambda value: value["sandbox"].update(enabled=False),
            "strong nested": lambda value: value["sandbox"].update(enableWeakerNestedSandbox=False),
            "unsandboxed fallback": lambda value: value["sandbox"].update(allowUnsandboxedCommands=True),
            "no fail closed": lambda value: value["sandbox"].update(failIfUnavailable=False),
            "missing credentials": lambda value: value["sandbox"].pop("credentials"),
            "missing read deny": lambda value: value["permissions"].update(deny=[]),
            "bypass enabled": lambda value: value["permissions"].update(disableBypassPermissionsMode="enable"),
            "hooks enabled": lambda value: value.update(disableAllHooks=False),
            "unmanaged hooks": lambda value: value.update(allowManagedHooksOnly=False),
            "MCP allowed": lambda value: value.update(allowedMcpServers=["sentinel"]),
            "unmanaged MCP": lambda value: value.update(allowManagedMcpServersOnly=False),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                settings = deepcopy(baseline)
                mutate(settings)
                settings_path, mcp_path = self._write_policy(
                    Path(temporary), settings, mcp
                )
                output = StringIO()
                with redirect_stdout(output):
                    valid = validate_managed_policy(settings_path, mcp_path)
                self.assertFalse(valid)
                self.assertEqual(output.getvalue(), "")

    def test_main_prints_only_boolean_for_invalid_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings, mcp = self._write_policy(
                Path(temporary),
                {"sentinel": "DO-NOT-PRINT-CREDENTIAL-BODY"},
                {"mcpServers": {"sentinel": {}}},
            )
            output = StringIO()

            status = main(settings, mcp, output)

            self.assertEqual(status, 1)
            self.assertEqual(output.getvalue(), "managed_policy_valid=false\n")
            self.assertNotIn("DO-NOT-PRINT", output.getvalue())

    def test_rejects_symlinked_special_broad_and_oversized_policy_files(self) -> None:
        baseline = json.loads(
            (ROOT / "profiles/claude/managed-settings.json").read_text()
        )
        mcp = {"mcpServers": {}}
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            real_settings, mcp_path = self._write_policy(fixture, baseline, mcp)

            linked_settings = fixture / "linked-settings.json"
            linked_settings.symlink_to(real_settings)
            self.assertFalse(validate_managed_policy(linked_settings, mcp_path))

            fifo_settings = fixture / "fifo-settings.json"
            os.mkfifo(fifo_settings, 0o600)
            self.assertFalse(validate_managed_policy(fifo_settings, mcp_path))

            real_settings.chmod(0o666)
            self.assertFalse(
                validate_managed_policy(
                    real_settings,
                    mcp_path,
                    expected_uid=os.getuid(),
                )
            )

            real_settings.chmod(0o600)
            real_settings.write_bytes(b" " * (64 * 1024 + 1))
            self.assertFalse(validate_managed_policy(real_settings, mcp_path))

    def test_enforces_expected_policy_owner_when_requested(self) -> None:
        baseline = json.loads(
            (ROOT / "profiles/claude/managed-settings.json").read_text()
        )
        with tempfile.TemporaryDirectory() as temporary:
            settings, mcp = self._write_policy(
                Path(temporary), baseline, {"mcpServers": {}}
            )

            self.assertTrue(
                validate_managed_policy(settings, mcp, expected_uid=os.getuid())
            )
            self.assertFalse(
                validate_managed_policy(settings, mcp, expected_uid=os.getuid() + 1)
            )


if __name__ == "__main__":
    unittest.main()
