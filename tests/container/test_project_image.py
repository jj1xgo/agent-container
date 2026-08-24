from pathlib import Path
import os
import tempfile
import unittest

from agent_container.project_image import ProjectImageConfig
from agent_container.project_image import load_project_image_config


class ProjectImageConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "workspace"
        self.workspace.mkdir()

    def test_loads_normalized_packages_and_node_version(self) -> None:
        config_dir = self.workspace / ".agent-container.d"
        config_dir.mkdir()
        (config_dir / "packages.txt").write_text(
            "# build deps\nmake\n gcc \nmake\nlibpng-dev=1.6.48-1\n",
            encoding="utf-8",
        )
        (config_dir / "node-version.txt").write_text(
            "22.23.1\n", encoding="utf-8"
        )

        config = load_project_image_config(self.workspace)

        self.assertEqual(config.packages, ("gcc", "libpng-dev=1.6.48-1", "make"))
        self.assertEqual(config.node_version, "22.23.1")
        self.assertFalse(config.is_empty)

    def test_absent_configuration_is_empty(self) -> None:
        self.assertEqual(
            load_project_image_config(self.workspace),
            ProjectImageConfig((), None),
        )
        self.assertTrue(load_project_image_config(self.workspace).is_empty)

    def test_rejects_invalid_configuration_contents(self) -> None:
        cases = {
            "unknown file": ("extra.txt", "x"),
            "leading option": ("packages.txt", "--allow-unauthenticated\n"),
            "shell separator": ("packages.txt", "make;id\n"),
            "command substitution": ("packages.txt", "$(id)\n"),
            "whitespace arguments": ("packages.txt", "make gcc\n"),
            "bad node": ("node-version.txt", "latest\n"),
            "multiple node lines": ("node-version.txt", "22.1.0\n22.2.0\n"),
        }
        for index, (label, (name, contents)) in enumerate(cases.items()):
            with self.subTest(label=label):
                workspace = Path(self.temporary.name) / f"case-{index}"
                config_dir = workspace / ".agent-container.d"
                config_dir.mkdir(parents=True)
                (config_dir / name).write_text(contents, encoding="utf-8")

                with self.assertRaises(ValueError):
                    load_project_image_config(workspace)

    def test_rejects_workspace_and_configuration_symlinks(self) -> None:
        real_workspace = Path(self.temporary.name) / "real-workspace"
        real_workspace.mkdir()
        linked_workspace = Path(self.temporary.name) / "linked-workspace"
        linked_workspace.symlink_to(real_workspace, target_is_directory=True)
        with self.assertRaises(ValueError):
            load_project_image_config(linked_workspace)

        real_config = Path(self.temporary.name) / "real-config"
        real_config.mkdir()
        (self.workspace / ".agent-container.d").symlink_to(
            real_config, target_is_directory=True
        )
        with self.assertRaises(ValueError):
            load_project_image_config(self.workspace)

    def test_rejects_allowed_and_unknown_file_symlinks(self) -> None:
        for index, name in enumerate(("packages.txt", "node-version.txt", "extra.txt")):
            with self.subTest(name=name):
                workspace = Path(self.temporary.name) / f"symlink-{index}"
                config_dir = workspace / ".agent-container.d"
                config_dir.mkdir(parents=True)
                target = workspace / "target"
                target.write_text("make\n", encoding="utf-8")
                (config_dir / name).symlink_to(target)

                with self.assertRaises(ValueError):
                    load_project_image_config(workspace)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO requires POSIX")
    def test_rejects_fifo_configuration_file(self) -> None:
        config_dir = self.workspace / ".agent-container.d"
        config_dir.mkdir()
        os.mkfifo(config_dir / "packages.txt")

        with self.assertRaises(ValueError):
            load_project_image_config(self.workspace)

    def test_rejects_legacy_claude_directory_without_reading_contents(self) -> None:
        legacy = self.workspace / ".claude-container.d"
        legacy.mkdir()
        secret = legacy / "secret"
        secret.write_text("do-not-read", encoding="utf-8")
        secret.chmod(0)
        self.addCleanup(secret.chmod, 0o600)

        with self.assertRaisesRegex(ValueError, r"\.claude-container\.d"):
            load_project_image_config(self.workspace)

    def test_rejects_oversized_and_invalid_utf8_files(self) -> None:
        for index, payload in enumerate((b"a" * (64 * 1024 + 1), b"\xff")):
            with self.subTest(index=index):
                workspace = Path(self.temporary.name) / f"payload-{index}"
                config_dir = workspace / ".agent-container.d"
                config_dir.mkdir(parents=True)
                (config_dir / "packages.txt").write_bytes(payload)

                with self.assertRaises(ValueError):
                    load_project_image_config(workspace)


if __name__ == "__main__":
    unittest.main()
