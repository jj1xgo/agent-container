import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from agent_container.broker.artifacts import RuntimeArtifacts
from agent_container.broker.runtime import bind_private_listener


LABEL = "test broker"


def _run_dir(root: Path) -> Path:
    run_dir = root / "run"
    run_dir.mkdir(mode=0o700)
    return run_dir


def _capability(run_dir: Path) -> Path:
    path = run_dir / "capability"
    path.write_text("c" * 43 + "\n", encoding="ascii")
    path.chmod(0o600)
    return path


def _bind(run_dir: Path) -> Path:
    path = run_dir / "broker.sock"
    bind_private_listener(path, backlog=1, label=LABEL).close()
    return path


def _tracked(run_dir: Path) -> RuntimeArtifacts:
    artifacts = RuntimeArtifacts.open(run_dir, label=LABEL)
    artifacts.track_file("capability")
    artifacts.track_socket("broker.sock")
    return artifacts


class RuntimeArtifactsTest(unittest.TestCase):
    def test_removes_file_then_socket_then_directory_and_closes_descriptor(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = _run_dir(Path(directory))
            _capability(run_dir)
            _bind(run_dir)
            artifacts = _tracked(run_dir)
            order: list[str] = []
            real_unlink = os.unlink

            def recording_unlink(name, *args, **kwargs):
                order.append(name)
                return real_unlink(name, *args, **kwargs)

            with mock.patch("os.unlink", side_effect=recording_unlink):
                self.assertFalse(artifacts.remove())
            self.assertEqual(order, ["capability", "broker.sock"])
            self.assertFalse(run_dir.exists())
            with self.assertRaises(OSError):
                os.fstat(artifacts._descriptor)
            self.assertFalse(artifacts.remove())

    def test_missing_artifacts_are_not_failures(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = _run_dir(Path(directory))
            artifacts = _tracked(run_dir)
            self.assertFalse(artifacts.remove())
            self.assertFalse(run_dir.exists())

    def test_open_requires_a_private_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = Path(directory) / "shared"
            run_dir.mkdir(mode=0o750)
            with self.assertRaisesRegex(PermissionError, "test broker run directory is not private"):
                RuntimeArtifacts.open(run_dir, label=LABEL)
            link = Path(directory) / "link"
            link.symlink_to(_run_dir(Path(directory)))
            with self.assertRaises(OSError):
                RuntimeArtifacts.open(link, label=LABEL)

    def test_track_rejects_a_name_of_the_wrong_type(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = _run_dir(Path(directory))
            _capability(run_dir)
            artifacts = RuntimeArtifacts.open(run_dir, label=LABEL)
            with self.assertRaisesRegex(ValueError, "test broker artifact type is invalid"):
                artifacts.track_socket("capability")
            artifacts.close()

    def test_replaced_file_is_kept_and_reported(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = _run_dir(Path(directory))
            capability = _capability(run_dir)
            _bind(run_dir)
            artifacts = _tracked(run_dir)
            capability.unlink()
            capability.mkdir()
            self.assertTrue(artifacts.remove())
            self.assertTrue(capability.is_dir())
            self.assertFalse((run_dir / "broker.sock").exists())
            self.assertTrue(run_dir.exists())
            capability.rmdir()
            self.assertFalse(artifacts.remove())
            self.assertFalse(run_dir.exists())

    def test_replaced_socket_inode_is_kept_even_when_it_is_a_socket(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = _run_dir(Path(directory))
            _capability(run_dir)
            socket_path = _bind(run_dir)
            artifacts = _tracked(run_dir)
            socket_path.unlink()
            _bind(run_dir)
            self.assertTrue(artifacts.remove())
            self.assertTrue(stat.S_ISSOCK(socket_path.lstat().st_mode))
            self.assertFalse((run_dir / "capability").exists())
            self.assertTrue(run_dir.exists())

    def test_name_that_appears_after_tracking_is_foreign(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = _run_dir(Path(directory))
            artifacts = _tracked(run_dir)
            (run_dir / "broker.sock").write_text("replacement", encoding="ascii")
            self.assertTrue(artifacts.remove())
            self.assertTrue((run_dir / "broker.sock").is_file())
            (run_dir / "broker.sock").unlink()
            self.assertFalse(artifacts.remove())
            self.assertFalse(run_dir.exists())

    def test_replaced_run_directory_is_kept(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = _run_dir(Path(directory))
            artifacts = _tracked(run_dir)
            moved = Path(directory) / "moved"
            run_dir.rename(moved)
            run_dir.mkdir(mode=0o700)
            self.assertTrue(artifacts.remove())
            self.assertTrue(run_dir.is_dir())
            self.assertTrue(moved.is_dir())
            artifacts.close()
