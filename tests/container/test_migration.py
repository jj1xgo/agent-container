from dataclasses import replace
import os
from pathlib import Path
import shutil
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_container.migration import GeneratedFile
from agent_container.migration import apply_claude_migration
from agent_container.migration import plan_claude_migration
from agent_container.migration import render_migration_plan


SECRET_MARKER = "DO-NOT-PRINT-CREDENTIAL-BODY"


class MigrationTestCase(unittest.TestCase):
    def make_source(self, root: Path) -> Path:
        source = root / "claude"
        (source / "hooks").mkdir(parents=True)
        (source / "skills/demo").mkdir(parents=True)
        (source / "CLAUDE.md").write_text("safe instructions\n", encoding="utf-8")
        (source / "settings.json").write_text(
            '{"permissions": {"allow": ["Read"]}}\n', encoding="utf-8"
        )
        (source / "hooks/run.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
        (source / "hooks/run.sh").chmod(0o751)
        (source / "skills/demo/SKILL.md").write_text("safe skill\n", encoding="utf-8")

        denied = (
            ".credentials.json",
            ".claude.json",
            "projects",
            "sessions",
            "transcripts",
            "handovers",
            "plans",
            "state",
            "cache",
            "logs",
            "test-results",
            "scratchpad",
            ".git",
        )
        for name in denied:
            path = source / name
            if "." in name and not name.startswith("test-") and name != ".git":
                path.write_text(SECRET_MARKER, encoding="utf-8")
            else:
                path.mkdir()
                (path / "secret").write_text(SECRET_MARKER, encoding="utf-8")
        return source

    def assert_no_stage(self, destination: Path) -> None:
        self.assertEqual(
            list(destination.parent.glob(f".{destination.name}.migrate-*")), []
        )


class MigrationPlanningTest(MigrationTestCase):
    def test_plan_selects_only_allowlisted_paths_and_render_hides_denied_bodies(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            plan = plan_claude_migration(source, root / "destination")

            self.assertEqual(
                {entry.relative_path.as_posix() for entry in plan.entries},
                {
                    "CLAUDE.md",
                    "settings.json",
                    "hooks",
                    "hooks/run.sh",
                    "skills",
                    "skills/demo",
                    "skills/demo/SKILL.md",
                },
            )
            self.assertEqual(
                tuple(entry.relative_path.as_posix() for entry in plan.entries),
                tuple(
                    sorted(entry.relative_path.as_posix() for entry in plan.entries)
                ),
            )
            rendered = render_migration_plan(plan)
            self.assertNotIn(SECRET_MARKER, "\n".join(rendered))
            self.assertIn("COPY executable hooks/run.sh", rendered)
            self.assertIn(f"DESTINATION {(root / 'destination').as_posix()}", rendered)
            self.assertTrue(
                {
                    "SKIP denied .credentials.json",
                    "SKIP denied .claude.json",
                    "SKIP denied .git",
                    "SKIP denied cache",
                    "SKIP denied handovers",
                    "SKIP denied logs",
                    "SKIP denied plans",
                    "SKIP denied projects",
                    "SKIP denied scratchpad",
                    "SKIP denied sessions",
                    "SKIP denied state",
                    "SKIP denied test-results",
                    "SKIP denied transcripts",
                }.issubset(rendered)
            )

    def test_settings_must_be_an_object(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            (source / "settings.json").write_text("[]", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "JSON object"):
                plan_claude_migration(source, root / "destination")

    def test_settings_reject_api_key_helper_at_every_nesting_level(self) -> None:
        for payload in (
            '{"apiKeyHelper": "do-not-leak"}',
            '{"nested": [{"apiKeyHelper": "do-not-leak"}]}',
        ):
            with self.subTest(payload=payload), TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                source = self.make_source(root)
                (source / "settings.json").write_text(payload, encoding="utf-8")

                with self.assertRaises(ValueError) as caught:
                    plan_claude_migration(source, root / "destination")
                self.assertIn("apiKeyHelper", str(caught.exception))
                self.assertNotIn("do-not-leak", str(caught.exception))

    def test_settings_reject_sensitive_environment_names_without_values(self) -> None:
        for name in (
            "ACCESS_TOKEN",
            "CLIENT_SECRET",
            "PASSWORD_FILE",
            "CREDENTIAL_PATH",
            "SERVICE_API_KEY",
            "AUTH_HEADER",
        ):
            with self.subTest(name=name), TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                source = self.make_source(root)
                (source / "settings.json").write_text(
                    '{"nested": [{"env": {"'
                    + name
                    + '": "do-not-leak"}}]}',
                    encoding="utf-8",
                )

                with self.assertRaises(ValueError) as caught:
                    plan_claude_migration(source, root / "destination")
                self.assertIn(name, str(caught.exception))
                self.assertNotIn("do-not-leak", str(caught.exception))

    def test_source_must_be_absolute(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute"):
            plan_claude_migration(Path("relative"), Path("/tmp/destination"))

    def test_source_must_not_be_a_symlink(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            real_source = self.make_source(root)
            source = root / "source-link"
            source.symlink_to(real_source, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink"):
                plan_claude_migration(source, root / "destination")

    def test_source_written_path_must_equal_its_strict_resolution(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            noncanonical = source / "hooks" / ".."

            with self.assertRaisesRegex(ValueError, "canonical"):
                plan_claude_migration(noncanonical, root / "destination")

    def test_allowlisted_symlink_is_rejected(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            (source / "hooks/run.sh").unlink()
            (source / "hooks/run.sh").symlink_to(source / "CLAUDE.md")

            with self.assertRaisesRegex(ValueError, "symlink"):
                plan_claude_migration(source, root / "destination")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO requires POSIX")
    def test_allowlisted_special_file_is_rejected(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            os.mkfifo(source / "hooks/pipe")

            with self.assertRaisesRegex(ValueError, "regular file or directory"):
                plan_claude_migration(source, root / "destination")

    def test_allowlisted_path_resolving_outside_source_is_rejected(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            outside = root / "outside"
            outside.write_text("outside", encoding="utf-8")
            hooks = source / "hooks"
            original_iterdir = Path.iterdir

            def escaping_iterdir(path: Path):
                if path == hooks:
                    return iter((hooks / ".." / ".." / "outside",))
                return original_iterdir(path)

            with patch.object(Path, "iterdir", escaping_iterdir):
                with self.assertRaisesRegex(ValueError, "escapes source"):
                    plan_claude_migration(source, root / "destination")

    def test_existing_destination_is_rejected(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            destination = root / "destination"
            destination.mkdir()

            with self.assertRaises(FileExistsError):
                plan_claude_migration(source, destination)


class MigrationApplyTest(MigrationTestCase):
    def test_apply_uses_private_modes_and_preserves_source(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            destination = root / "destination"
            source_bytes = (source / "hooks/run.sh").read_bytes()
            source_mode = stat.S_IMODE((source / "hooks/run.sh").stat().st_mode)
            plan = plan_claude_migration(source, destination)
            plan = replace(
                plan,
                generated_files=(
                    GeneratedFile(Path("generated/config.json"), b"{}\n"),
                    GeneratedFile(Path("generated/run.sh"), b"#!/bin/sh\n", True),
                ),
            )

            result = apply_claude_migration(plan)

            self.assertEqual(result, destination)
            for path in (
                destination,
                destination / "hooks",
                destination / "skills",
                destination / "skills/demo",
                destination / "generated",
            ):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((destination / "CLAUDE.md").stat().st_mode), 0o600
            )
            self.assertEqual(
                stat.S_IMODE((destination / "hooks/run.sh").stat().st_mode), 0o700
            )
            self.assertEqual(
                stat.S_IMODE((destination / "generated/config.json").stat().st_mode),
                0o600,
            )
            self.assertEqual(
                stat.S_IMODE((destination / "generated/run.sh").stat().st_mode),
                0o700,
            )
            self.assertEqual((source / "hooks/run.sh").read_bytes(), source_bytes)
            self.assertEqual(
                stat.S_IMODE((source / "hooks/run.sh").stat().st_mode), source_mode
            )
            self.assert_no_stage(destination)

    def test_apply_leaves_destination_absent_when_source_file_becomes_symlink(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            destination = root / "destination"
            plan = plan_claude_migration(source, destination)
            (source / "CLAUDE.md").unlink()
            (source / "CLAUDE.md").symlink_to(source / "settings.json")

            with self.assertRaisesRegex(ValueError, "symlink"):
                apply_claude_migration(plan)
            self.assertFalse(destination.exists())
            self.assert_no_stage(destination)

    def test_apply_rejects_settings_that_become_sensitive_after_planning(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            destination = root / "destination"
            plan = plan_claude_migration(source, destination)
            (source / "settings.json").write_text(
                '{"env": {"ACCESS_TOKEN": "do-not-leak"}}', encoding="utf-8"
            )

            with self.assertRaises(ValueError) as caught:
                apply_claude_migration(plan)
            self.assertNotIn("do-not-leak", str(caught.exception))
            self.assertFalse(destination.exists())
            self.assert_no_stage(destination)

    def test_apply_leaves_late_existing_destination_unchanged(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            destination = root / "destination"
            plan = plan_claude_migration(source, destination)
            destination.mkdir()
            marker = destination / "marker"
            marker.write_text("keep", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                apply_claude_migration(plan)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            self.assert_no_stage(destination)

    def test_apply_cleans_stage_after_copy_failure(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = self.make_source(root)
            destination = root / "destination"
            plan = plan_claude_migration(source, destination)

            with patch(
                "agent_container.migration.shutil.copyfile",
                side_effect=OSError("copy failed"),
            ):
                with self.assertRaisesRegex(OSError, "copy failed"):
                    apply_claude_migration(plan)
            self.assertFalse(destination.exists())
            self.assert_no_stage(destination)


if __name__ == "__main__":
    unittest.main()
