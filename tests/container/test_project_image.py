from pathlib import Path
import os
import tempfile
import unittest

from agent_container.project_image import ProjectImageConfig
from agent_container.project_image import load_project_image_config
from agent_container.project_image import project_image_key
from agent_container.project_image import project_image_name
from agent_container.project_image import write_project_build_context


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


class ProjectImageIdentityTest(unittest.TestCase):
    def test_identity_is_deterministic_and_covers_all_inputs(self) -> None:
        config = ProjectImageConfig(("gcc", "make"), "22.23.1")
        first = project_image_key("sha256:base", config, "amd64")
        same = project_image_key("sha256:base", config, "amd64")

        self.assertEqual(first, same)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        for changed in (
            project_image_key("sha256:other", config, "amd64"),
            project_image_key(
                "sha256:base", ProjectImageConfig(("make",), "22.23.1"), "amd64"
            ),
            project_image_key("sha256:base", config, "arm64"),
        ):
            self.assertNotEqual(first, changed)
        self.assertEqual(
            project_image_name("sotlas-frontend", first),
            f"localhost/agent-container-project:sotlas-frontend-{first[:16]}",
        )

    def test_identity_rejects_values_unsafe_for_hashing_or_image_names(self) -> None:
        config = ProjectImageConfig((), None)
        bad_keys = (
            ("", "amd64"),
            ("sha256:base\nother", "amd64"),
            ("sha256:base", "../amd64"),
        )
        for base, architecture in bad_keys:
            with self.subTest(base=base, architecture=architecture):
                with self.assertRaises(ValueError):
                    project_image_key(base, config, architecture)

        for project_id, key in (("../project", "a" * 64), ("project", "bad")):
            with self.subTest(project_id=project_id, key=key):
                with self.assertRaises(ValueError):
                    project_image_name(project_id, key)


class ProjectImageBuildContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "context"
        self.root.mkdir()

    def test_writes_minimal_packages_and_node_context(self) -> None:
        sentinel = Path(self.temporary.name) / "DO-NOT-COPY-SECRET"
        sentinel.write_text("secret sentinel", encoding="utf-8")
        config = ProjectImageConfig(("gcc", "make"), "22.23.1")

        containerfile = write_project_build_context(
            self.root, "localhost/agent-container:dev", config
        )

        self.assertEqual(containerfile, self.root / "Containerfile")
        self.assertEqual(
            {path.name for path in self.root.iterdir()},
            {"Containerfile", "packages.txt"},
        )
        self.assertEqual(
            (self.root / "packages.txt").read_text(encoding="utf-8"),
            "gcc\nmake\n",
        )
        body = containerfile.read_text(encoding="utf-8")
        self.assertTrue(body.startswith("ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n"))
        self.assertIn("USER root", body)
        self.assertIn("USER agent", body)
        self.assertIn(
            "xargs --no-run-if-empty apt-get install -y --no-install-recommends",
            body,
        )
        self.assertIn("https://nodejs.org/dist/v22.23.1/", body)
        self.assertIn("SHASUMS256.txt", body)
        self.assertIn("sha256sum --check --strict", body)
        self.assertIn("/opt/project-node", body)
        self.assertIn(
            "PATH=/opt/project-node/bin:/usr/local/bin:/opt/agent-node/bin:", body
        )
        self.assertNotIn("secret sentinel", body)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        for generated in self.root.iterdir():
            self.assertEqual(generated.stat().st_mode & 0o777, 0o600)

    def test_omits_optional_files_and_node_layer_for_empty_config(self) -> None:
        containerfile = write_project_build_context(
            self.root, "localhost/agent-container:dev", ProjectImageConfig((), None)
        )

        self.assertEqual({path.name for path in self.root.iterdir()}, {"Containerfile"})
        body = containerfile.read_text(encoding="utf-8")
        self.assertNotIn("packages.txt", body)
        self.assertNotIn("/opt/project-node", body)

    def test_rejects_unsafe_context_or_constructed_config(self) -> None:
        occupied = Path(self.temporary.name) / "occupied"
        occupied.mkdir()
        (occupied / "existing").write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            write_project_build_context(
                occupied, "localhost/agent-container:dev", ProjectImageConfig((), None)
            )
        with self.assertRaises(ValueError):
            write_project_build_context(
                self.root,
                "localhost/agent-container:dev",
                ProjectImageConfig(("make;id",), None),
            )


if __name__ == "__main__":
    unittest.main()
