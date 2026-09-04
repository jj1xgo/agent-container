import os
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest import mock

from agent_container.broker.capability import CAPABILITY_PATTERN
from agent_container.broker.capability import connect_unix
from agent_container.broker.capability import read_capability
from agent_container.broker.capability import validate_exact_path
from agent_container.broker.capability import validate_socket


LABEL = "test capability"


class ValidateExactPathTest(unittest.TestCase):
    def test_accepts_only_absolute_resolved_existing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "target"
            target.write_text("x", encoding="ascii")
            self.assertEqual(validate_exact_path(target, label=LABEL), target)

            link = root / "link"
            link.symlink_to(target)
            for path in (Path("relative"), link, root / "missing"):
                with self.subTest(path=path), self.assertRaises(ValueError) as raised:
                    validate_exact_path(path, label=LABEL)
                self.assertEqual(str(raised.exception), "test capability is invalid")


class ReadCapabilityTest(unittest.TestCase):
    def test_pattern_is_exactly_43_url_safe_characters(self) -> None:
        self.assertIsNotNone(CAPABILITY_PATTERN.fullmatch("A" * 43))
        self.assertIsNone(CAPABILITY_PATTERN.fullmatch("A" * 42))
        self.assertIsNone(CAPABILITY_PATTERN.fullmatch("A" * 44))
        self.assertIsNone(CAPABILITY_PATTERN.fullmatch("A" * 42 + "+"))

    def test_reads_only_exact_private_current_user_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            capability = root / "capability"
            capability.write_text("c" * 43 + "\n", encoding="ascii")
            capability.chmod(0o600)

            self.assertEqual(read_capability(capability, label=LABEL), "c" * 43)

            link = root / "link"
            link.symlink_to(capability)
            for path in (Path("capability"), link):
                with self.subTest(path=path), self.assertRaises(ValueError) as raised:
                    read_capability(path, label=LABEL)
                self.assertEqual(str(raised.exception), "test capability is invalid")

            capability.chmod(0o644)
            with self.assertRaises(ValueError):
                read_capability(capability, label=LABEL)
            capability.chmod(0o600)
            with mock.patch("os.getuid", return_value=os.getuid() + 1), self.assertRaises(
                ValueError
            ):
                read_capability(capability, label=LABEL)

            directory_path = root / "directory"
            directory_path.mkdir(mode=0o700)
            with self.assertRaises(ValueError):
                read_capability(directory_path, label=LABEL)

    def test_rejects_wrong_size_missing_newline_and_non_ascii(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capability = Path(directory).resolve() / "capability"
            for body in (
                "c" * 43,
                "c" * 44 + "\n",
                "c" * 42 + "\n\n",
                "é" * 21 + "c\n",
                "c" * 42 + "+\n",
            ):
                capability.write_bytes(body.encode("utf-8"))
                capability.chmod(0o600)
                with self.subTest(body=body), self.assertRaises(ValueError) as raised:
                    read_capability(capability, label=LABEL)
                self.assertEqual(str(raised.exception), "test capability is invalid")

    def test_rejects_path_replaced_after_descriptor_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capability = Path(directory).resolve() / "capability"
            capability.write_text("c" * 43 + "\n", encoding="ascii")
            capability.chmod(0o600)
            real_open = os.open

            def open_then_replace(path: Path, flags: int) -> int:
                descriptor = real_open(path, flags)
                capability.unlink()
                capability.write_text("d" * 43 + "\n", encoding="ascii")
                capability.chmod(0o600)
                return descriptor

            with mock.patch("os.open", side_effect=open_then_replace), self.assertRaises(
                ValueError
            ):
                read_capability(capability, label=LABEL)

    def test_opens_with_nonblocking_nofollow_flags_and_rejects_fifo(self) -> None:
        if not hasattr(os, "O_NONBLOCK") or not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("platform does not expose safe Unix open flags")
        with tempfile.TemporaryDirectory() as directory:
            capability = Path(directory).resolve() / "capability"
            os.mkfifo(capability, mode=0o600)
            real_open = os.open
            seen_flags: list[int] = []

            def checked_open(path: Path, flags: int) -> int:
                seen_flags.append(flags)
                return real_open(path, flags)

            with mock.patch("os.open", side_effect=checked_open), self.assertRaises(ValueError):
                read_capability(capability, label=LABEL)
            self.assertEqual(len(seen_flags), 1)
            self.assertTrue(seen_flags[0] & os.O_NONBLOCK)
            self.assertTrue(seen_flags[0] & os.O_NOFOLLOW)


class ValidateSocketTest(unittest.TestCase):
    def test_requires_socket_type_private_mode_and_current_user(self) -> None:
        path = Path("/run/agent-test/broker.sock")
        valid = os.stat_result(
            (stat.S_IFSOCK | 0o600, 0, 0, 1, os.getuid(), 0, 0, 0, 0, 0)
        )
        with mock.patch.object(Path, "stat", return_value=valid):
            self.assertEqual(validate_socket(path, label="test socket"), path)
        invalid = (
            os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, os.getuid(), 0, 0, 0, 0, 0)),
            os.stat_result((stat.S_IFSOCK | 0o660, 0, 0, 1, os.getuid(), 0, 0, 0, 0, 0)),
            os.stat_result((stat.S_IFSOCK | 0o600, 0, 0, 1, os.getuid() + 1, 0, 0, 0, 0, 0)),
        )
        for metadata in invalid:
            with self.subTest(mode=metadata.st_mode, uid=metadata.st_uid), mock.patch.object(
                Path, "stat", return_value=metadata
            ), self.assertRaises(ValueError) as raised:
                validate_socket(path, label="test socket")
            self.assertEqual(str(raised.exception), "test socket is invalid")

    def test_does_not_resolve_the_path_itself(self) -> None:
        path = Path("/run/agent-test/broker.sock")
        valid = os.stat_result(
            (stat.S_IFSOCK | 0o600, 0, 0, 1, os.getuid(), 0, 0, 0, 0, 0)
        )
        with mock.patch.object(Path, "stat", return_value=valid), mock.patch.object(
            Path, "resolve", side_effect=AssertionError("must not resolve")
        ):
            self.assertEqual(validate_socket(path, label="test socket"), path)


class FakeSocket:
    def __init__(self, connect_error: OSError | None = None) -> None:
        self.connect_error = connect_error
        self.timeout: float | None = None
        self.connected: str | None = None
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def connect(self, path: str) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = path

    def close(self) -> None:
        self.closed = True


class ConnectUnixTest(unittest.TestCase):
    def test_creates_unix_stream_socket_sets_timeout_and_connects(self) -> None:
        created: list[tuple[object, object]] = []
        fake = FakeSocket()

        def factory(family: object, kind: object) -> FakeSocket:
            created.append((family, kind))
            return fake

        client = connect_unix(Path("/run/agent-test/broker.sock"), timeout=30, socket_factory=factory)
        self.assertIs(client, fake)
        self.assertEqual(created, [(socket.AF_UNIX, socket.SOCK_STREAM)])
        self.assertEqual(fake.timeout, 30)
        self.assertEqual(fake.connected, "/run/agent-test/broker.sock")
        self.assertFalse(fake.closed)

    def test_closes_socket_and_reraises_when_connect_fails(self) -> None:
        fake = FakeSocket(connect_error=FileNotFoundError("private-socket-marker"))
        with self.assertRaises(FileNotFoundError):
            connect_unix(Path("/run/agent-test/broker.sock"), timeout=30, socket_factory=lambda *_: fake)
        self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()
