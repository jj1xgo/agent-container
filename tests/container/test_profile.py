from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_container.profile import seed_codex_home


ROOT = Path(__file__).resolve().parents[2]


class ProfileSeedTest(unittest.TestCase):
    def test_seed_copies_managed_files_and_records_version(self) -> None:
        with TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            seed_codex_home(ROOT / "profiles/codex", codex_home)
            self.assertTrue((codex_home / "config.toml").is_file())
            self.assertTrue((codex_home / "hooks.json").is_file())
            self.assertTrue((codex_home / "skills/handover/SKILL.md").is_file())
            self.assertEqual(
                (codex_home / "managed-profile.version").read_text(encoding="utf-8"),
                "1\n",
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
