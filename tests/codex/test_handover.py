from datetime import datetime
from datetime import timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import subprocess
import sys
import unittest

from agent_container.handover import create_handover
from agent_container.handover import latest_handover
from agent_container.handover import validate_project_id


class HandoverTest(unittest.TestCase):
    def test_validate_project_id_accepts_repository_style_slug(self) -> None:
        self.assertEqual(validate_project_id("agent-container"), "agent-container")

    def test_validate_project_id_rejects_path_traversal(self) -> None:
        for value in ("../secret", "family/project", "", "."):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_project_id(value)

    def test_latest_handover_returns_newest_matching_regular_file(self) -> None:
        with TemporaryDirectory() as temp:
            project_dir = Path(temp) / "agent-container"
            project_dir.mkdir()
            older = project_dir / "2026-08-21_1758.md"
            newest = project_dir / "2026-08-22_1815.md"
            older.write_text("- Created: 2026-08-21T17:58+00:00\n", encoding="utf-8")
            newest.write_text("- Created: 2026-08-22T18:15+00:00\n", encoding="utf-8")
            (project_dir / "notes.md").write_text("ignore", encoding="utf-8")

            self.assertEqual(latest_handover(Path(temp), "agent-container"), newest)

    def test_latest_handover_uses_created_instant_across_timezones(self) -> None:
        with TemporaryDirectory() as temp:
            project = Path(temp) / "agent-container"
            project.mkdir()
            misleading = project / "2026-08-25_1236.md"
            actual_latest = project / "2026-08-25_0835.md"
            misleading.write_text(
                "- Created: 2026-08-25T12:36+09:00\n", encoding="utf-8"
            )
            actual_latest.write_text(
                "- Created: 2026-08-25T08:35+00:00\n", encoding="utf-8"
            )

            self.assertEqual(
                latest_handover(Path(temp), "agent-container"), actual_latest
            )

    def test_latest_handover_ignores_project_directory_symlink(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "root"
            outside = Path(temp) / "outside"
            root.mkdir()
            outside.mkdir()
            outside_file = outside / "2026-08-22_1815.md"
            outside_file.write_text("outside", encoding="utf-8")
            (root / "agent-container").symlink_to(outside, target_is_directory=True)

            self.assertIsNone(latest_handover(root, "agent-container"))

    def test_create_handover_uses_expected_path_and_metadata(self) -> None:
        with TemporaryDirectory() as temp:
            path = create_handover(
                root=Path(temp),
                project_id="agent-container",
                title="Codex運用設計",
                session_id="thread-example",
                now=datetime(2026, 8, 22, 18, 15, tzinfo=timezone.utc),
            )

            self.assertRegex(path.name, r"^2026-08-22_181500_[0-9a-f]{8}\.md$")
            body = path.read_text(encoding="utf-8")
            self.assertIn("# Handover: Codex運用設計", body)
            self.assertIn("- Project: agent-container", body)
            self.assertIn("- Session: thread-example", body)
            self.assertIn("- Created: 2026-08-22T18:15:00+00:00", body)
            self.assertIn("## 次の一手", body)

    def test_create_handover_is_unique_within_same_second(self) -> None:
        with TemporaryDirectory() as temp:
            now = datetime(2026, 8, 22, 18, 15, tzinfo=timezone.utc)
            first = create_handover(Path(temp), "agent-container", "first", "", now)
            second = create_handover(Path(temp), "agent-container", "second", "", now)
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())
            os.utime(first, ns=(1_000_000_000, 1_000_000_000))
            os.utime(second, ns=(2_000_000_000, 2_000_000_000))
            self.assertEqual(
                latest_handover(Path(temp), "agent-container"), second
            )

    def test_create_handover_normalizes_non_utc_timestamp(self) -> None:
        with TemporaryDirectory() as temp:
            jst = timezone(timedelta(hours=9))
            path = create_handover(
                Path(temp),
                "agent-container",
                "timezone",
                "session",
                datetime(2026, 8, 25, 12, 36, tzinfo=jst),
            )
            self.assertTrue(path.name.startswith("2026-08-25_033600_"))
            self.assertIn(
                "- Created: 2026-08-25T03:36:00+00:00",
                path.read_text(encoding="utf-8"),
            )

    def test_create_handover_rejects_naive_timestamp(self) -> None:
        with TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "timezone"):
                create_handover(
                    Path(temp),
                    "agent-container",
                    "naive",
                    "session",
                    datetime(2026, 8, 25, 12, 36),
                )

    def test_create_handover_rejects_project_directory_symlink(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp) / "root"
            outside = Path(temp) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "agent-container").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(ValueError) as raised:
                create_handover(root, "agent-container", "title", "session")

            self.assertEqual(str(raised.exception), "project directory must not be a symlink")
            self.assertEqual(list(outside.iterdir()), [])


class HandoverCliTest(unittest.TestCase):
    def test_create_command_prints_created_path(self) -> None:
        with TemporaryDirectory() as temp:
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_container.handover_cli",
                    "create",
                    "--root",
                    temp,
                    "--project",
                    "agent-container",
                    "--title",
                    "運用引き継ぎ",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            created = Path(result.stdout.strip())
            self.assertTrue(created.is_file())
            self.assertEqual(created.parent.name, "agent-container")


if __name__ == "__main__":
    unittest.main()
