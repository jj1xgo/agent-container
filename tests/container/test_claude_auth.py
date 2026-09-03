import hmac
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_container.claude_auth import discard_staged_token
from agent_container.claude_auth import install_claude_token
from agent_container.claude_auth import quarantine_legacy_claude_state
from agent_container.claude_auth import stage_claude_token
from agent_container.claude_auth import validate_legacy_quarantine_sources
from agent_container.state import StateLayout


class ClaudeAuthTest(unittest.TestCase):
    def make_layout(self, root: Path) -> StateLayout:
        auth_dir = root / "shared-auth/claude"
        auth_dir.mkdir(parents=True, mode=0o700)
        auth_dir.chmod(0o700)
        return StateLayout(root, "agent-container")

    def make_private_file(self, path: Path, contents: str = "sk-ant-oat01-" + "x" * 95) -> None:
        path.write_text(contents, encoding="ascii")
        path.chmod(0o600)

    def test_install_atomically_replaces_existing_token(self) -> None:
        with TemporaryDirectory() as temp:
            auth_dir = Path(temp) / "shared-auth/claude"
            auth_dir.mkdir(parents=True, mode=0o700)
            auth_dir.chmod(0o700)
            old = auth_dir / "oauth-token"
            self.make_private_file(old, "sk-ant-oat01-" + "o" * 95)

            staged = stage_claude_token(auth_dir, "sk-ant-oat01-" + "n" * 95)

            self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o600)
            install_claude_token(staged, old)
            self.assertTrue(
                hmac.compare_digest(old.read_text(encoding="ascii"), "sk-ant-oat01-" + "n" * 95)
            )
            self.assertFalse(staged.exists())

    def test_stage_cleans_generated_file_after_final_read_failure(self) -> None:
        with TemporaryDirectory() as temp:
            auth_dir = Path(temp) / "shared-auth/claude"
            auth_dir.mkdir(parents=True, mode=0o700)
            auth_dir.chmod(0o700)

            with patch.object(Path, "read_text", side_effect=OSError("read failure")):
                with self.assertRaises(OSError):
                    stage_claude_token(auth_dir, "sk-ant-oat01-" + "x" * 95)

            self.assertFalse(any(auth_dir.iterdir()))

    def test_invalid_token_creates_no_staging_file(self) -> None:
        with TemporaryDirectory() as temp:
            auth_dir = Path(temp) / "shared-auth/claude"
            auth_dir.mkdir(parents=True, mode=0o700)
            auth_dir.chmod(0o700)

            with self.assertRaises(ValueError):
                stage_claude_token(auth_dir, "x" * 31)

            self.assertEqual(list(auth_dir.iterdir()), [])

    def test_discard_removes_only_the_exact_staging_file(self) -> None:
        with TemporaryDirectory() as temp:
            auth_dir = Path(temp) / "shared-auth/claude"
            auth_dir.mkdir(parents=True, mode=0o700)
            auth_dir.chmod(0o700)
            staged = stage_claude_token(auth_dir, "sk-ant-oat01-" + "x" * 95)
            other = stage_claude_token(auth_dir, "sk-ant-oat01-" + "y" * 95)

            discard_staged_token(staged)

            self.assertFalse(staged.exists())
            self.assertTrue(other.is_file())

    def test_install_rejects_symlinked_destination_and_wrong_parent_mode(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            auth_dir = root / "shared-auth/claude"
            auth_dir.mkdir(parents=True, mode=0o700)
            auth_dir.chmod(0o700)
            staged = stage_claude_token(auth_dir, "sk-ant-oat01-" + "n" * 95)
            target = root / "target"
            self.make_private_file(target, "sk-ant-oat01-" + "o" * 95)
            destination = auth_dir / "oauth-token"
            destination.symlink_to(target)

            with self.assertRaises(ValueError):
                install_claude_token(staged, destination)

            self.assertTrue(staged.exists())
            self.assertTrue(
                hmac.compare_digest(target.read_text(encoding="ascii"), "sk-ant-oat01-" + "o" * 95)
            )

            destination.unlink()
            auth_dir.chmod(0o755)
            with self.assertRaises(PermissionError):
                install_claude_token(staged, destination)

            self.assertTrue(staged.exists())
            self.assertFalse(destination.exists())

    def test_moves_only_allowlisted_legacy_entries_without_reading_bodies(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            layout = self.make_layout(root)
            self.make_private_file(layout.claude_token_file)
            self.make_private_file(layout.claude_legacy_credentials_file, "c" * 32)
            self.make_private_file(layout.claude_legacy_metadata_file, "m" * 32)
            layout.claude_legacy_backups.mkdir(mode=0o700)
            nested = layout.claude_legacy_backups / "old"
            nested.write_text("legacy", encoding="ascii")
            unrelated = layout.claude_auth_dir / "unrelated"
            unrelated.write_text("keep", encoding="ascii")
            sources = validate_legacy_quarantine_sources(layout)

            with patch.object(Path, "read_text", side_effect=AssertionError):
                quarantine = quarantine_legacy_claude_state(layout, sources, "fixed-nonce")

            self.assertEqual(quarantine, root / "quarantine/claude/fixed-nonce")
            self.assertEqual(
                {entry.name for entry in quarantine.iterdir()},
                {".credentials.json", ".claude.json", "backups"},
            )
            self.assertTrue(layout.claude_token_file.is_file())
            self.assertTrue(unrelated.is_file())

    def test_no_legacy_entries_is_a_noop(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            layout = self.make_layout(root)
            sources = validate_legacy_quarantine_sources(layout)

            self.assertEqual(sources, ())
            self.assertIsNone(quarantine_legacy_claude_state(layout, sources))
            self.assertFalse(layout.claude_quarantine_root.exists())

    def test_rejects_symlinked_source_or_symlink_inside_backups_before_move(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            layout = self.make_layout(root)
            target = root / "target"
            target.write_text("legacy", encoding="ascii")
            layout.claude_legacy_credentials_file.symlink_to(target)

            with self.assertRaises(ValueError):
                quarantine_legacy_claude_state(
                    layout, (layout.claude_legacy_credentials_file,)
                )

            self.assertTrue(layout.claude_legacy_credentials_file.is_symlink())
            layout.claude_legacy_credentials_file.unlink()
            layout.claude_legacy_backups.mkdir(mode=0o700)
            (layout.claude_legacy_backups / "target").write_text(
                "legacy", encoding="ascii"
            )
            (layout.claude_legacy_backups / "link").symlink_to(
                layout.claude_legacy_backups / "target"
            )

            with self.assertRaises(ValueError):
                quarantine_legacy_claude_state(
                    layout, (layout.claude_legacy_backups,)
                )

            self.assertTrue(layout.claude_legacy_backups.exists())

    def test_quarantine_tree_is_private_and_active_token_is_untouched(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            layout = self.make_layout(root)
            self.make_private_file(layout.claude_token_file, "sk-ant-oat01-" + "t" * 95)
            self.make_private_file(layout.claude_legacy_credentials_file, "c" * 32)
            layout.claude_legacy_backups.mkdir(mode=0o700)
            nested = layout.claude_legacy_backups / "old"
            nested.write_text("legacy", encoding="ascii")
            nested.chmod(0o644)
            sources = validate_legacy_quarantine_sources(layout)

            quarantine = quarantine_legacy_claude_state(layout, sources, "fixed-nonce")

            assert quarantine is not None
            directories = (
                root / "quarantine",
                layout.claude_quarantine_root,
                quarantine,
                quarantine / "backups",
            )
            for directory in directories:
                with self.subTest(directory=directory):
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((quarantine / ".credentials.json").stat().st_mode), 0o600
            )
            self.assertEqual(stat.S_IMODE((quarantine / "backups/old").stat().st_mode), 0o600)
            self.assertTrue(
                hmac.compare_digest(
                    layout.claude_token_file.read_text(encoding="ascii"), "sk-ant-oat01-" + "t" * 95
                )
            )

    def test_quarantine_preserves_nested_backup_directories(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            layout = self.make_layout(root)
            layout.claude_legacy_backups.mkdir(mode=0o700)
            nested = layout.claude_legacy_backups / "session"
            nested.mkdir(mode=0o755)
            old = nested / "old"
            old.write_text("legacy", encoding="ascii")
            old.chmod(0o644)
            sources = validate_legacy_quarantine_sources(layout)

            quarantine = quarantine_legacy_claude_state(layout, sources, "fixed-nonce")

            assert quarantine is not None
            self.assertTrue((quarantine / "backups/session/old").is_file())
            self.assertEqual(
                stat.S_IMODE((quarantine / "backups/session").stat().st_mode), 0o700
            )
            self.assertEqual(
                stat.S_IMODE((quarantine / "backups/session/old").stat().st_mode), 0o600
            )

    def test_quarantine_revalidates_the_validated_source_set_before_move(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            layout = self.make_layout(root)
            self.make_private_file(layout.claude_legacy_credentials_file, "c" * 32)
            sources = validate_legacy_quarantine_sources(layout)
            target = root / "target"
            target.write_text("legacy", encoding="ascii")
            layout.claude_legacy_credentials_file.unlink()
            layout.claude_legacy_credentials_file.symlink_to(target)

            with self.assertRaises(ValueError):
                quarantine_legacy_claude_state(layout, sources)

            self.assertTrue(layout.claude_legacy_credentials_file.is_symlink())


if __name__ == "__main__":
    unittest.main()
