from pathlib import Path
from tempfile import TemporaryDirectory
import tomllib
import unittest
from unittest.mock import patch

from agent_container.profile import seed_codex_home
from agent_container.profile import update_codex_handover_profile
from agent_container.profile import validate_codex_handover_profile


ROOT = Path(__file__).resolve().parents[2]


class ProfileSeedTest(unittest.TestCase):
    def test_seed_copies_managed_files_and_records_version(self) -> None:
        with TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            seed_codex_home(ROOT / "profiles/codex", codex_home)
            self.assertTrue((codex_home / "config.toml").is_file())
            self.assertTrue((codex_home / "hooks.json").is_file())
            self.assertEqual(
                (codex_home / "rules/default.rules").read_text(encoding="utf-8"),
                (
                    'prefix_rule(pattern=["gh", "pr", "view"], decision="allow")\n'
                    'prefix_rule(pattern=["gh", "pr", "list"], decision="allow")\n'
                    'prefix_rule(pattern=["gh", "pr", "checks"], decision="allow")\n'
                    'prefix_rule(pattern=["gh", "pr", "status"], decision="allow")\n'
                    'prefix_rule(pattern=["gh", "issue", "view"], decision="allow")\n'
                    'prefix_rule(pattern=["gh", "issue", "list"], decision="allow")\n'
                    'prefix_rule(pattern=["gh", "run", "view"], decision="allow")\n'
                    'prefix_rule(pattern=["gh", "run", "list"], decision="allow")\n'
                    'prefix_rule(pattern=["gh", "repo", "view"], decision="allow")\n'
                    'prefix_rule(pattern=["agent-handover", "create"], decision="allow")\n'
                ),
            )
            self.assertTrue((codex_home / "skills/handover/SKILL.md").is_file())
            self.assertEqual(
                (codex_home / "managed-profile.version").read_text(encoding="utf-8"),
                "5\n",
            )

    def test_seed_refuses_to_overwrite_existing_rules(self) -> None:
        with TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            rules = codex_home / "rules"
            rules.mkdir(parents=True)
            (rules / "default.rules").write_text("existing\n", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "rules"):
                seed_codex_home(ROOT / "profiles/codex", codex_home)
            self.assertEqual(
                (rules / "default.rules").read_text(encoding="utf-8"), "existing\n"
            )

    def test_seed_refuses_to_overwrite_existing_config(self) -> None:
        with TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text("existing\n", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "config.toml"):
                seed_codex_home(ROOT / "profiles/codex", codex_home)
            self.assertEqual(
                (codex_home / "config.toml").read_text(encoding="utf-8"), "existing\n"
            )

    def test_update_handover_profile_preserves_custom_rules_and_is_idempotent(self) -> None:
        with TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            seed_codex_home(ROOT / "profiles/codex", codex_home)
            rules_file = codex_home / "rules/default.rules"
            rules_file.write_text("custom-rule\n", encoding="utf-8")
            skill_file = codex_home / "skills/handover/SKILL.md"
            skill_file.write_text("old managed skill\n", encoding="utf-8")

            update_codex_handover_profile(ROOT / "profiles/codex", codex_home)
            update_codex_handover_profile(ROOT / "profiles/codex", codex_home)

            rules = rules_file.read_text(encoding="utf-8")
            self.assertIn("custom-rule\n", rules)
            self.assertEqual(rules.count('agent-handover", "create'), 1)
            self.assertEqual(
                skill_file.read_text(encoding="utf-8"),
                (ROOT / "profiles/codex/skills/handover/SKILL.md").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertEqual(
                (codex_home / "managed-profile.version").read_text(
                    encoding="utf-8"
                ),
                "5\n",
            )

    # Break caught: a project seeded before the sandbox network setting keeps
    # running Codex tool commands without network, so agent-family and the
    # broker git remote fail with EPERM although the shipped profile is fixed.
    def test_update_profile_adds_managed_sandbox_network_to_old_config(self) -> None:
        with TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            seed_codex_home(ROOT / "profiles/codex", codex_home)
            config_file = codex_home / "config.toml"
            config_file.write_text(
                'cli_auth_credentials_store = "file"\n'
                'model = "custom-model"\n'
                "\n[tui]\n"
                'status_line = ["git-branch"]\n',
                encoding="utf-8",
            )

            update_codex_handover_profile(ROOT / "profiles/codex", codex_home)
            update_codex_handover_profile(ROOT / "profiles/codex", codex_home)

            text = config_file.read_text(encoding="utf-8")
            config = tomllib.loads(text)
            self.assertIs(config["sandbox_workspace_write"]["network_access"], True)
            self.assertEqual(config["model"], "custom-model")
            self.assertEqual(config["tui"]["status_line"], ["git-branch"])
            self.assertEqual(text.count("[sandbox_workspace_write]"), 1)
            self.assertEqual(
                (codex_home / "managed-profile.version").read_text(encoding="utf-8"),
                "5\n",
            )

    def test_update_profile_overrides_disabled_sandbox_network_in_place(self) -> None:
        with TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            seed_codex_home(ROOT / "profiles/codex", codex_home)
            config_file = codex_home / "config.toml"
            config_file.write_text(
                'model = "custom-model"\n'
                "\n[sandbox_workspace_write]\n"
                "network_access = false\n"
                'writable_roots = ["/tmp/extra"]\n'
                "\n[tui]\n"
                'status_line = ["git-branch"]\n',
                encoding="utf-8",
            )

            update_codex_handover_profile(ROOT / "profiles/codex", codex_home)

            config = tomllib.loads(config_file.read_text(encoding="utf-8"))
            self.assertIs(config["sandbox_workspace_write"]["network_access"], True)
            self.assertEqual(
                config["sandbox_workspace_write"]["writable_roots"], ["/tmp/extra"]
            )
            self.assertEqual(config["tui"]["status_line"], ["git-branch"])

    def test_update_profile_rejects_symlinked_config(self) -> None:
        with TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            seed_codex_home(ROOT / "profiles/codex", codex_home)
            config_file = codex_home / "config.toml"
            config_file.unlink()
            outside = Path(temp) / "outside.toml"
            outside.write_text('model = "outside"\n', encoding="utf-8")
            config_file.symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                update_codex_handover_profile(ROOT / "profiles/codex", codex_home)

            self.assertEqual(outside.read_text(encoding="utf-8"), 'model = "outside"\n')

    def test_update_handover_profile_rejects_symlinked_rules(self) -> None:
        with TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            seed_codex_home(ROOT / "profiles/codex", codex_home)
            rules_file = codex_home / "rules/default.rules"
            rules_file.unlink()
            outside = Path(temp) / "outside.rules"
            outside.write_text("outside\n", encoding="utf-8")
            rules_file.symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                update_codex_handover_profile(ROOT / "profiles/codex", codex_home)

            self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

    # Break caught: a project seeded by managed profile version 1 (before the
    # approval rules existed) has no rules directory, so update-profile raised
    # FileNotFoundError and the project could never receive later profile fixes.
    def test_update_profile_seeds_missing_rules_from_version_one_home(self) -> None:
        with TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            seed_codex_home(ROOT / "profiles/codex", codex_home)
            for path in (codex_home / "rules/default.rules", codex_home / "rules"):
                path.unlink() if path.is_file() else path.rmdir()
            config_file = codex_home / "config.toml"
            config_file.write_text(
                'cli_auth_credentials_store = "file"\n'
                "\n[tui]\n"
                'status_line = ["git-branch"]\n'
                '\n[plugins."custom@dev"]\n'
                "enabled = true\n",
                encoding="utf-8",
            )
            (codex_home / "managed-profile.version").write_text("1\n", encoding="utf-8")

            update_codex_handover_profile(ROOT / "profiles/codex", codex_home)
            update_codex_handover_profile(ROOT / "profiles/codex", codex_home)

            rules_file = codex_home / "rules/default.rules"
            self.assertFalse(rules_file.is_symlink())
            self.assertEqual(
                rules_file.read_text(encoding="utf-8"),
                (ROOT / "profiles/codex/rules/default.rules").read_text(
                    encoding="utf-8"
                ),
            )
            config = tomllib.loads(config_file.read_text(encoding="utf-8"))
            self.assertIs(config["sandbox_workspace_write"]["network_access"], True)
            self.assertEqual(config["tui"]["status_line"], ["git-branch"])
            self.assertIs(config["plugins"]["custom@dev"]["enabled"], True)
            self.assertEqual(
                (codex_home / "managed-profile.version").read_text(encoding="utf-8"),
                "5\n",
            )

    def test_update_profile_rejects_symlinked_rules_directory_when_seeding(self) -> None:
        with TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            seed_codex_home(ROOT / "profiles/codex", codex_home)
            (codex_home / "rules/default.rules").unlink()
            (codex_home / "rules").rmdir()
            outside = Path(temp) / "outside-rules"
            outside.mkdir()
            (codex_home / "rules").symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                update_codex_handover_profile(ROOT / "profiles/codex", codex_home)

            self.assertEqual(list(outside.iterdir()), [])

    def test_validate_handover_profile_accepts_current_version(self) -> None:
        with TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            seed_codex_home(ROOT / "profiles/codex", codex_home)

            validate_codex_handover_profile(codex_home)

    def test_validate_handover_profile_rejects_old_version_with_update_command(
        self,
    ) -> None:
        with TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            seed_codex_home(ROOT / "profiles/codex", codex_home)
            (codex_home / "managed-profile.version").write_text(
                "4\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(
                ValueError, r"agentctl project update-profile PROJECT"
            ):
                validate_codex_handover_profile(codex_home)

    def test_update_profile_rejects_symlinked_skill_ancestor_before_writes(
        self,
    ) -> None:
        with TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            seed_codex_home(ROOT / "profiles/codex", codex_home)
            version_file = codex_home / "managed-profile.version"
            version_file.write_text("4\n", encoding="utf-8")
            rules_file = codex_home / "rules/default.rules"
            original_rules = rules_file.read_text(encoding="utf-8")
            skill_directory = codex_home / "skills"
            outside = Path(temp) / "outside-skills"
            skill_directory.rename(outside)
            skill_directory.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                update_codex_handover_profile(ROOT / "profiles/codex", codex_home)

            self.assertEqual(rules_file.read_text(encoding="utf-8"), original_rules)
            self.assertEqual(version_file.read_text(encoding="utf-8"), "4\n")

    def test_update_failure_does_not_advance_profile_version(self) -> None:
        with TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            seed_codex_home(ROOT / "profiles/codex", codex_home)
            version_file = codex_home / "managed-profile.version"
            version_file.write_text("4\n", encoding="utf-8")

            with patch(
                "agent_container.profile.shutil.copy2",
                side_effect=OSError("private-copy-failure"),
            ):
                with self.assertRaises(OSError):
                    update_codex_handover_profile(
                        ROOT / "profiles/codex", codex_home
                    )

            self.assertEqual(version_file.read_text(encoding="utf-8"), "4\n")
