from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest import mock

from agent_container.broker.capability import CAPABILITY_PATTERN
from agent_container.broker.runtime import MAX_UNIX_SOCKET_PATH_BYTES
from agent_container.broker.runtime import allocate_run_dir
from agent_container.broker.runtime import bind_private_listener
from agent_container.broker.runtime import create_private_file
from agent_container.broker.runtime import generate_capability
from agent_container.broker.runtime import remove_runtime_artifacts


LABEL = "test broker"


class CreatePrivateFileTest(unittest.TestCase):
    def test_creates_exclusive_private_ascii_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capability"
            create_private_file(path, "abc\n", label=LABEL)
            self.assertEqual(path.read_bytes(), b"abc\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                create_private_file(path, "again\n", label=LABEL)

    def test_refuses_symlink_target_and_short_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                create_private_file(link, "abc\n", label=LABEL)
            self.assertFalse(target.exists())

            path = root / "short"
            with mock.patch("os.write", return_value=0), self.assertRaisesRegex(
                OSError, "test broker private file write failed"
            ):
                create_private_file(path, "abc\n", label=LABEL)


class AllocateRunDirTest(unittest.TestCase):
    def test_allocates_a_private_hex_named_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_id, run_dir = allocate_run_dir(Path(directory), label=LABEL)
            self.assertRegex(run_id, r"^[0-9a-f]{16}$")
            self.assertEqual(run_dir, Path(directory) / run_id)
            self.assertTrue(run_dir.is_dir())
            self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)

    def test_retries_collisions_then_gives_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "aaaaaaaaaaaaaaaa").mkdir(mode=0o700)
            with mock.patch(
                "secrets.token_hex", side_effect=["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"]
            ):
                run_id, run_dir = allocate_run_dir(root, label=LABEL)
            self.assertEqual(run_id, "bbbbbbbbbbbbbbbb")
            self.assertTrue(run_dir.is_dir())

            with mock.patch("secrets.token_hex", return_value="aaaaaaaaaaaaaaaa"), self.assertRaisesRegex(
                FileExistsError, "could not allocate test broker runtime"
            ):
                allocate_run_dir(root, label=LABEL, attempts=3)


class GenerateCapabilityTest(unittest.TestCase):
    def test_generates_43_url_safe_characters(self) -> None:
        capability = generate_capability(label=LABEL)
        self.assertEqual(len(capability), 43)
        self.assertIsNotNone(CAPABILITY_PATTERN.fullmatch(capability))

    def test_rejects_unexpected_token_shape(self) -> None:
        with mock.patch("secrets.token_urlsafe", return_value="short"), self.assertRaisesRegex(
            RuntimeError, "generated test broker capability has invalid format"
        ):
            generate_capability(label=LABEL)


class BindPrivateListenerTest(unittest.TestCase):
    def test_binds_a_private_listening_unix_socket(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl-") as directory:
            path = Path(directory) / "broker.sock"
            listener = bind_private_listener(path, backlog=4, label=LABEL)
            try:
                metadata = path.lstat()
                self.assertTrue(stat.S_ISSOCK(metadata.st_mode))
                self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    client.connect(str(path))
                finally:
                    client.close()
            finally:
                listener.close()

    def test_rejects_long_paths_and_existing_paths(self) -> None:
        self.assertEqual(MAX_UNIX_SOCKET_PATH_BYTES, 107)
        long_path = Path("/" + "a" * 120)
        with self.assertRaisesRegex(ValueError, "test broker socket path is too long"):
            bind_private_listener(long_path, backlog=4, label=LABEL)
        with tempfile.TemporaryDirectory(prefix="bl-") as directory:
            path = Path(directory) / "broker.sock"
            path.write_text("replacement", encoding="ascii")
            with self.assertRaisesRegex(FileExistsError, "test broker socket path already exists"):
                bind_private_listener(path, backlog=4, label=LABEL)

    def test_bind_failure_closes_socket_and_only_unlinks_a_socket(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl-") as directory:
            path = Path(directory) / "broker.sock"
            fake = mock.Mock()
            fake.bind.side_effect = OSError("bind failed")
            with mock.patch("socket.socket", return_value=fake), self.assertRaises(OSError):
                bind_private_listener(path, backlog=4, label=LABEL)
            fake.close.assert_called_once_with()
            self.assertFalse(path.exists())

    def test_chmod_receives_the_path_object(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl-") as directory:
            path = Path(directory) / "broker.sock"
            fake = mock.Mock()
            with mock.patch("socket.socket", return_value=fake), mock.patch("os.chmod") as chmod:
                self.assertIs(bind_private_listener(path, backlog=7, label=LABEL), fake)
            fake.bind.assert_called_once_with(str(path))
            chmod.assert_called_once_with(path, 0o600)
            fake.listen.assert_called_once_with(7)


class RemoveRuntimeArtifactsTest(unittest.TestCase):
    def _layout(self, root: Path) -> tuple[Path, Path, Path]:
        run_dir = root / "run"
        run_dir.mkdir(mode=0o700)
        capability = run_dir / "capability"
        capability.write_text("c" * 43 + "\n", encoding="ascii")
        capability.chmod(0o600)
        return run_dir, capability, run_dir / "broker.sock"

    def test_removes_capability_socket_and_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir, capability, socket_path = self._layout(Path(directory))
            listener = bind_private_listener(socket_path, backlog=1, label=LABEL)
            listener.close()
            failed = remove_runtime_artifacts(
                capability_path=capability, socket_path=socket_path, run_dir=run_dir
            )
            self.assertFalse(failed)
            self.assertFalse(run_dir.exists())

    def test_missing_artifacts_are_not_failures(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir(mode=0o700)
            failed = remove_runtime_artifacts(
                capability_path=run_dir / "capability",
                socket_path=run_dir / "broker.sock",
                run_dir=run_dir,
            )
            self.assertFalse(failed)
            self.assertFalse(run_dir.exists())

    def test_refuses_replaced_capability_and_keeps_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir, capability, socket_path = self._layout(Path(directory))
            capability.unlink()
            capability.mkdir()
            failed = remove_runtime_artifacts(
                capability_path=capability, socket_path=socket_path, run_dir=run_dir
            )
            self.assertTrue(failed)
            self.assertTrue(capability.is_dir())
            self.assertTrue(run_dir.exists())

    def test_refuses_replaced_socket_but_still_removes_capability(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ra-") as directory:
            run_dir, capability, socket_path = self._layout(Path(directory))
            socket_path.write_text("replacement", encoding="ascii")
            failed = remove_runtime_artifacts(
                capability_path=capability, socket_path=socket_path, run_dir=run_dir
            )
            self.assertTrue(failed)
            self.assertFalse(capability.exists())
            self.assertTrue(socket_path.is_file())
            self.assertTrue(run_dir.exists())


if __name__ == "__main__":
    unittest.main()
