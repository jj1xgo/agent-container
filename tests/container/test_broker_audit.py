import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from agent_container.broker.audit import AuditLog


LABEL = "test audit"


class AuditLogTest(unittest.TestCase):
    def test_validate_creates_a_private_empty_file_and_append_writes_ascii_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            log = AuditLog(path, label=LABEL)

            log.validate()

            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_bytes(), b"")

            log.append({"timestamp": "t1", "status": "ok", "path": "/x"})
            log.append({"timestamp": "t2", "status": "denied"})

            self.assertEqual(
                path.read_bytes(),
                b'{"timestamp":"t1","status":"ok","path":"/x"}\n'
                b'{"timestamp":"t2","status":"denied"}\n',
            )

    def test_append_escapes_non_ascii_and_preserves_key_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            AuditLog(path, label=LABEL).append({"b": "é", "a": 1})
            self.assertEqual(path.read_bytes(), b'{"b":"\\u00e9","a":1}\n')

    def test_rejects_symlink_fifo_directory_and_wrong_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("", encoding="ascii")
            target.chmod(0o600)
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "test audit file must be a regular non-symlink file"):
                AuditLog(link, label=LABEL).validate()

            fifo = root / "fifo"
            os.mkfifo(fifo, 0o600)
            with self.assertRaisesRegex(ValueError, "regular non-symlink"):
                AuditLog(fifo, label=LABEL).validate()

            folder = root / "folder"
            folder.mkdir(mode=0o700)
            with self.assertRaisesRegex(ValueError, "regular non-symlink"):
                AuditLog(folder, label=LABEL).validate()

            wrong_mode = root / "wrong-mode"
            wrong_mode.write_text("", encoding="ascii")
            wrong_mode.chmod(0o644)
            with self.assertRaisesRegex(PermissionError, "test audit file must have mode 0600"):
                AuditLog(wrong_mode, label=LABEL).validate()

    def test_opens_with_nonblocking_append_nofollow_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            real_open = os.open
            seen: list[int] = []

            def checked_open(target: object, flags: int, *args: object, **kwargs: object) -> int:
                if Path(str(target)) == path:
                    seen.append(flags)
                return real_open(target, flags, *args, **kwargs)

            with mock.patch("os.open", side_effect=checked_open):
                AuditLog(path, label=LABEL).validate()

            self.assertEqual(len(seen), 1)
            for flag in (os.O_WRONLY, os.O_APPEND, os.O_CREAT, os.O_NOFOLLOW, os.O_NONBLOCK):
                self.assertTrue(seen[0] & flag)

    def test_rejects_foreign_owner_and_replacement_during_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text("", encoding="ascii")
            path.chmod(0o600)
            real_fstat = os.fstat

            def foreign_owner(descriptor: int) -> os.stat_result:
                metadata = real_fstat(descriptor)
                values = list(metadata)
                values[4] = os.getuid() + 1
                return os.stat_result(values)

            with mock.patch("os.fstat", side_effect=foreign_owner), self.assertRaisesRegex(
                PermissionError, "test audit file must be owned by the current user"
            ):
                AuditLog(path, label=LABEL).validate()

            real_stat = os.stat

            def replaced(target: object, *args: object, **kwargs: object) -> os.stat_result:
                metadata = real_stat(target, *args, **kwargs)
                if Path(str(target)) == path and kwargs.get("follow_symlinks") is False:
                    values = list(metadata)
                    values[1] = metadata.st_ino + 1
                    return os.stat_result(values)
                return metadata

            with mock.patch("os.stat", side_effect=replaced), self.assertRaisesRegex(
                ValueError, "test audit file changed during validation"
            ):
                AuditLog(path, label=LABEL).validate()

    def test_descriptor_is_closed_when_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text("", encoding="ascii")
            path.chmod(0o644)
            real_close = os.close
            closed: list[int] = []

            def tracked_close(descriptor: int) -> None:
                closed.append(descriptor)
                real_close(descriptor)

            with mock.patch("os.close", side_effect=tracked_close), self.assertRaises(PermissionError):
                AuditLog(path, label=LABEL).validate()

            self.assertEqual(len(closed), 1)

    def test_append_rejects_short_writes_and_leaves_no_partial_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            with mock.patch("os.write", return_value=0), self.assertRaisesRegex(
                OSError, "test audit write failed"
            ):
                AuditLog(path, label=LABEL).append({"a": 1})
            self.assertEqual(path.read_bytes(), b"")

    def test_append_retries_partial_writes_and_fsyncs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            real_write = os.write
            calls: list[int] = []

            def partial(descriptor: int, body: bytes) -> int:
                calls.append(len(body))
                return real_write(descriptor, body[:3])

            with mock.patch("os.write", side_effect=partial), mock.patch("os.fsync") as fsync:
                AuditLog(path, label=LABEL).append({"a": 1})

            self.assertEqual(path.read_bytes(), b'{"a":1}\n')
            self.assertGreater(len(calls), 1)
            fsync.assert_called_once()


if __name__ == "__main__":
    unittest.main()
