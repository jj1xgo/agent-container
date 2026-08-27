from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_container.claude_policy import main
from agent_container.claude_policy import validate_managed_policy


ROOT = Path(__file__).resolve().parents[2]
MANAGED_INSTRUCTIONS = """# Managed Claude handover workflow

別 session または別 agent へ作業を引き継ぐ必要がある場合だけ handover を作成する。
作成前に Git status、直近 commit、実行済み test、未解決事項を確認し、推測した成功を書かない。

handover の作成には次の command だけを使い、完成した以下の 7 section を固定順序で各 1 回だけ stdin へ渡す。

`agent-handover create --title "引き継ぎタイトル"`

## 作業の目的

完了した内容を書く。

## 現在地

完了した内容を書く。

## 決定事項と理由

完了した内容を書く。

## 変更したファイル・commit・PR

完了した内容を書く。

## 検証結果

完了した内容を書く。

## 未解決事項とリスク

完了した内容を書く。

## 次の一手

完了した内容を書く。

credential、token、環境値、transcript 全文を含めない。broker が拒否した場合は停止し、sandbox や mount を弱めず、別 path への直接書き込みや fallback を行わない。
""".encode("utf-8")


class ClaudeManagedPolicyTest(unittest.TestCase):
    def _write_policy(self, root: Path, settings: dict, mcp: dict) -> tuple[Path, Path]:
        settings_path = root / "managed-settings.json"
        mcp_path = root / "managed-mcp.json"
        settings_path.write_text(json.dumps(settings), encoding="utf-8")
        mcp_path.write_text(json.dumps(mcp), encoding="utf-8")
        settings_path.chmod(0o600)
        mcp_path.chmod(0o600)
        instructions_path = root / "CLAUDE.md"
        instructions_path.write_bytes(MANAGED_INSTRUCTIONS)
        instructions_path.chmod(0o644)
        return settings_path, mcp_path

    def test_accepts_repository_managed_policy(self) -> None:
        self.assertTrue(
            validate_managed_policy(
                ROOT / "profiles/claude/managed-settings.json",
                ROOT / "profiles/claude/managed-mcp.json",
            )
        )

    def test_rejects_tampered_missing_and_wrong_mode_managed_instructions(self) -> None:
        baseline = json.loads(
            (ROOT / "profiles/claude/managed-settings.json").read_text()
        )
        expected = (ROOT / "profiles/claude/CLAUDE.md").read_bytes()
        for failure in ("tampered", "missing", "mode"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                settings, mcp = self._write_policy(
                    Path(temporary), baseline, {"mcpServers": {}}
                )
                instructions = settings.parent / "CLAUDE.md"
                instructions.write_bytes(expected)
                instructions.chmod(0o644)
                if failure == "tampered":
                    instructions.write_bytes(expected + b"tampered\n")
                elif failure == "missing":
                    instructions.unlink()
                else:
                    instructions.chmod(0o600)

                self.assertFalse(
                    validate_managed_policy(
                        settings,
                        mcp,
                        instructions,
                        expected_uid=os.getuid(),
                    )
                )

    def test_rejects_special_and_symlinked_managed_instructions(self) -> None:
        baseline = json.loads(
            (ROOT / "profiles/claude/managed-settings.json").read_text()
        )
        expected = (ROOT / "profiles/claude/CLAUDE.md").read_bytes()
        for failure in ("fifo", "symlink"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                settings, mcp = self._write_policy(
                    Path(temporary), baseline, {"mcpServers": {}}
                )
                instructions = settings.parent / "CLAUDE.md"
                instructions.unlink()
                if failure == "fifo":
                    os.mkfifo(instructions, 0o644)
                else:
                    target = settings.parent / "real-CLAUDE.md"
                    target.write_bytes(expected)
                    target.chmod(0o644)
                    instructions.symlink_to(target)

                self.assertFalse(
                    validate_managed_policy(
                        settings,
                        mcp,
                        instructions,
                        expected_uid=os.getuid(),
                    )
                )

    def test_rejects_wrong_owner_managed_instructions_independently(self) -> None:
        baseline = json.loads(
            (ROOT / "profiles/claude/managed-settings.json").read_text()
        )
        with tempfile.TemporaryDirectory() as temporary:
            settings, mcp = self._write_policy(
                Path(temporary), baseline, {"mcpServers": {}}
            )
            instructions = settings.parent / "CLAUDE.md"
            real_fstat = os.fstat

            def wrong_instruction_owner(descriptor: int):
                metadata = real_fstat(descriptor)
                try:
                    opened_path = os.readlink(f"/proc/self/fd/{descriptor}")
                except OSError:
                    return metadata
                if opened_path != str(instructions):
                    return metadata
                fields = list(metadata)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)

            self.assertTrue(
                validate_managed_policy(
                    settings,
                    mcp,
                    instructions,
                    expected_uid=os.getuid(),
                )
            )
            with patch(
                "agent_container.claude_policy.os.fstat",
                side_effect=wrong_instruction_owner,
            ):
                self.assertFalse(
                    validate_managed_policy(
                        settings,
                        mcp,
                        instructions,
                        expected_uid=os.getuid(),
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

    def test_rejects_policy_files_beneath_a_symlinked_directory(self) -> None:
        baseline = json.loads(
            (ROOT / "profiles/claude/managed-settings.json").read_text()
        )
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            real_directory = fixture / "real-policy"
            real_directory.mkdir()
            settings, mcp = self._write_policy(
                real_directory, baseline, {"mcpServers": {}}
            )
            linked_directory = fixture / "linked-policy"
            linked_directory.symlink_to(real_directory, target_is_directory=True)

            self.assertFalse(
                validate_managed_policy(
                    linked_directory / settings.name,
                    linked_directory / mcp.name,
                )
            )

    def test_rejects_writable_policy_directory_when_owner_is_required(self) -> None:
        baseline = json.loads(
            (ROOT / "profiles/claude/managed-settings.json").read_text()
        )
        with tempfile.TemporaryDirectory() as temporary:
            policy_directory = Path(temporary) / "policy"
            policy_directory.mkdir()
            settings, mcp = self._write_policy(
                policy_directory, baseline, {"mcpServers": {}}
            )
            policy_directory.chmod(0o777)

            self.assertFalse(
                validate_managed_policy(
                    settings,
                    mcp,
                    expected_uid=os.getuid(),
                )
            )


if __name__ == "__main__":
    unittest.main()
