from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_container.profile import seed_codex_home
from agent_container.profile import update_codex_handover_profile


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
                "3\n",
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
                "3\n",
            )

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
