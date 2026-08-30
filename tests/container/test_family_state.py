import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

import agent_container.family_state as family_state
from agent_container.family_state import FamilyBinding
from agent_container.family_state import load_family_binding
from agent_container.family_state import write_family_binding
from agent_container.state import Repository


class FamilyBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.parent = self.root / "family" / "projects" / "demo"
        for directory in (
            self.root / "family",
            self.root / "family" / "projects",
            self.parent,
        ):
            directory.mkdir(exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        self.path = self.parent / "binding.json"
        self.binding = FamilyBinding(Repository("family", "roadmap"), 12345)

    def _write_private_bytes(self, body: bytes) -> None:
        self.path.write_bytes(body)
        self.path.chmod(0o600)

    def _temporary_siblings(self) -> list[Path]:
        return [
            path
            for path in self.parent.glob(".binding.json.*")
            if path.name != ".binding.json.lock"
        ]

    def test_round_trips_an_exact_repository_binding(self) -> None:
        write_family_binding(self.path, self.binding)

        self.assertEqual(load_family_binding(self.path), self.binding)
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")),
            {"repository": "family/roadmap", "repository_id": 12345},
        )

    def test_rejects_malformed_and_non_exact_json_schema(self) -> None:
        cases = (
            b'{"repository":"family/roadmap","repository":"other/repo","repository_id":12345}',
            b'{"repository":"family/roadmap","repository_id":12345,"extra":true}',
            b'{"repository":"family/roadmap"}',
            b'{"repository":"family/roadmap","repository_id":12345',
            b'\xff',
        )
        for body in cases:
            with self.subTest(body=body):
                self._write_private_bytes(body)
                with self.assertRaisesRegex(ValueError, "family binding is invalid"):
                    load_family_binding(self.path)
                self.path.unlink()

    def test_rejects_invalid_identifiers_and_non_normalized_repository(self) -> None:
        cases = (
            {"repository": "family/roadmap", "repository_id": True},
            {"repository": "family/roadmap", "repository_id": 0},
            {"repository": "family/roadmap", "repository_id": -1},
            {"repository": "family/roadmap/", "repository_id": 1},
            {"repository": "Family/roadmap", "repository_id": 1},
            {"repository": 42, "repository_id": 1},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                self._write_private_bytes(json.dumps(payload).encode("ascii"))
                with self.assertRaises(ValueError):
                    load_family_binding(self.path)
                self.path.unlink()

    def test_rejects_symlinked_ancestor_or_file(self) -> None:
        target = self.root / "target"
        target.mkdir(mode=0o700)
        target.chmod(0o700)
        linked_parent = self.root / "linked"
        linked_parent.symlink_to(target, target_is_directory=True)
        linked_path = linked_parent / "binding.json"
        with self.assertRaises(ValueError):
            load_family_binding(linked_path)

        self._write_private_bytes(b'{"repository":"family/roadmap","repository_id":12345}')
        link = self.parent / "link.json"
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            load_family_binding(link)

    def test_rejects_non_private_ancestor_directory(self) -> None:
        self._write_private_bytes(b'{"repository":"family/roadmap","repository_id":12345}')
        (self.root / "family").chmod(0o755)

        with self.assertRaises(PermissionError):
            load_family_binding(self.path)

    def test_rejects_fifo_hard_link_wrong_mode_and_wrong_owner(self) -> None:
        fifo = self.parent / "fifo.json"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(ValueError):
            load_family_binding(fifo)
        fifo.unlink()

        self._write_private_bytes(b'{"repository":"family/roadmap","repository_id":12345}')
        os.link(self.path, self.parent / "other.json")
        with self.assertRaises(ValueError):
            load_family_binding(self.path)
        (self.parent / "other.json").unlink()
        self.path.chmod(0o644)
        with self.assertRaises(PermissionError):
            load_family_binding(self.path)
        self.path.chmod(0o600)
        real_fstat = os.fstat
        with patch("agent_container.family_state.os.fstat") as fake_fstat:
            fake_fstat.side_effect = lambda fd: _with_uid(real_fstat(fd), os.getuid() + 1)
            with self.assertRaises(PermissionError):
                load_family_binding(self.path)

    def test_read_rejects_entry_replaced_after_open(self) -> None:
        self._write_private_bytes(b'{"repository":"family/roadmap","repository_id":12345}')
        real_stat = os.stat
        calls = 0

        def replace_on_second_stat(*args: object, **kwargs: object) -> os.stat_result:
            nonlocal calls
            result = real_stat(*args, **kwargs)
            if args[0] == self.path.name:
                calls += 1
            if calls == 2:
                return _with_inode(result, result.st_ino + 1)
            return result

        with patch("agent_container.family_state.os.stat", side_effect=replace_on_second_stat):
            with self.assertRaises(ValueError):
                load_family_binding(self.path)

    def test_write_rejects_a_publish_collision_without_creating_a_binding(self) -> None:
        with patch("agent_container.family_state.os.link", side_effect=FileExistsError):
            with self.assertRaises(FileExistsError):
                write_family_binding(self.path, self.binding)

        self.assertFalse(self.path.exists())
        self.assertEqual(self._temporary_siblings(), [])

    def test_write_replaces_only_a_valid_explicitly_observed_binding(self) -> None:
        existing = FamilyBinding(Repository("family", "existing"), 9)
        write_family_binding(self.path, existing)

        write_family_binding(self.path, self.binding)

        self.assertEqual(load_family_binding(self.path), self.binding)

    def test_write_refuses_to_replace_an_invalid_existing_binding(self) -> None:
        self._write_private_bytes(b'{"repository":"family/roadmap","repository_id":0}')
        before = self.path.read_bytes()

        with self.assertRaises(ValueError):
            write_family_binding(self.path, self.binding)

        self.assertEqual(self.path.read_bytes(), before)

    def test_write_completes_partial_writes(self) -> None:
        real_write = os.write

        def partial_write(descriptor: int, body: bytes) -> int:
            return real_write(descriptor, body[:3])

        with patch("agent_container.family_state.os.write", side_effect=partial_write):
            write_family_binding(self.path, self.binding)

        self.assertEqual(load_family_binding(self.path), self.binding)

    def test_write_preserves_existing_binding_when_file_or_parent_fsync_fails(self) -> None:
        existing = FamilyBinding(Repository("family", "existing"), 9)
        write_family_binding(self.path, existing)
        before = self.path.read_bytes()
        real_fsync = os.fsync

        for failure_after in (1, 2):
            with self.subTest(failure_after=failure_after):
                calls = 0

                def fail_fsync(descriptor: int) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == failure_after:
                        raise OSError("injected fsync failure")
                    real_fsync(descriptor)

                with patch("agent_container.family_state.os.fsync", side_effect=fail_fsync):
                    with self.assertRaises(OSError):
                        write_family_binding(self.path, self.binding)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self._temporary_siblings(), [])

    def test_write_reports_post_publication_fsync_failure_with_new_binding_cleaned_up(self) -> None:
        existing = FamilyBinding(Repository("family", "existing"), 9)
        write_family_binding(self.path, existing)
        real_fsync = os.fsync
        calls = 0

        def fail_post_publication_fsync(descriptor: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("injected post-publication fsync failure")
            real_fsync(descriptor)

        with patch(
            "agent_container.family_state.os.fsync",
            side_effect=fail_post_publication_fsync,
        ):
            with self.assertRaises(OSError):
                write_family_binding(self.path, self.binding)

        self.assertEqual(load_family_binding(self.path), self.binding)
        self.assertEqual(self._temporary_siblings(), [])

    def test_create_reports_each_fsync_failure_with_defined_published_state(self) -> None:
        real_fsync = os.fsync
        for failure_after, published in ((1, False), (2, True)):
            with self.subTest(failure_after=failure_after):
                calls = 0

                def fail_fsync(descriptor: int) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == failure_after:
                        raise OSError("injected fsync failure")
                    real_fsync(descriptor)

                with patch(
                    "agent_container.family_state.os.fsync", side_effect=fail_fsync
                ):
                    with self.assertRaises(OSError):
                        write_family_binding(self.path, self.binding)

                self.assertEqual(self.path.exists(), published)
                if published:
                    self.assertEqual(load_family_binding(self.path), self.binding)
                    self.path.unlink()
                self.assertEqual(self._temporary_siblings(), [])

    def test_write_does_not_overwrite_a_binding_swapped_before_publication(self) -> None:
        write_family_binding(self.path, FamilyBinding(Repository("family", "old"), 1))
        concurrent = FamilyBinding(Repository("family", "concurrent"), 2)
        replacement = self.parent / ".concurrent-replacement"
        replacement.write_text(
            json.dumps(
                {"repository": concurrent.repository.slug, "repository_id": 2}
            ),
            encoding="ascii",
        )
        replacement.chmod(0o600)
        real_fsync = os.fsync
        calls = 0

        def swap_after_final_check(descriptor: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                os.replace(replacement, self.path)
            real_fsync(descriptor)

        with patch(
            "agent_container.family_state.os.fsync", side_effect=swap_after_final_check
        ):
            with self.assertRaises(ValueError):
                write_family_binding(self.path, self.binding)

        self.assertEqual(load_family_binding(self.path), concurrent)
        self.assertEqual(self._temporary_siblings(), [])

    def test_concurrent_writers_serialize_before_observing_the_binding(self) -> None:
        write_family_binding(self.path, FamilyBinding(Repository("family", "old"), 1))
        first = FamilyBinding(Repository("family", "first"), 2)
        second = FamilyBinding(Repository("family", "second"), 3)
        observed = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        second_reached_snapshot = threading.Event()
        failures: list[BaseException] = []
        real_snapshot = family_state._snapshot

        def pause_first_snapshot(*args: object, **kwargs: object) -> object:
            result = real_snapshot(*args, **kwargs)
            if threading.current_thread().name == "first-writer":
                observed.set()
                release_first.wait(timeout=5)
            elif threading.current_thread().name == "second-writer":
                second_reached_snapshot.set()
            return result

        def run(binding: FamilyBinding, started: threading.Event | None = None) -> None:
            try:
                if started is not None:
                    started.set()
                write_family_binding(self.path, binding)
            except BaseException as error:
                failures.append(error)

        with patch(
            "agent_container.family_state._snapshot", side_effect=pause_first_snapshot
        ):
            first_thread = threading.Thread(
                target=run, args=(first,), name="first-writer"
            )
            first_thread.start()
            self.assertTrue(observed.wait(timeout=5))
            second_thread = threading.Thread(
                target=run, args=(second, second_started), name="second-writer"
            )
            second_thread.start()
            self.assertTrue(second_started.wait(timeout=5))
            try:
                self.assertFalse(second_reached_snapshot.wait(timeout=0.2))
            finally:
                release_first.set()
                first_thread.join(timeout=5)
                second_thread.join(timeout=5)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(load_family_binding(self.path), second)

    def test_recovery_restores_external_swap_before_a_queued_writer_updates(self) -> None:
        write_family_binding(self.path, FamilyBinding(Repository("family", "old"), 1))
        external = FamilyBinding(Repository("family", "external"), 2)
        queued = FamilyBinding(Repository("family", "queued"), 3)
        replacement = self.parent / ".external-replacement"
        replacement.write_text(
            json.dumps(
                {"repository": external.repository.slug, "repository_id": 2}
            ),
            encoding="ascii",
        )
        replacement.chmod(0o600)
        queued_started = threading.Event()
        queued_failures: list[BaseException] = []

        def write_queued() -> None:
            queued_started.set()
            try:
                write_family_binding(self.path, queued)
            except BaseException as error:
                queued_failures.append(error)

        real_fsync = os.fsync
        calls = 0
        queued_thread: threading.Thread | None = None

        def swap_and_queue_writer(descriptor: int) -> None:
            nonlocal calls, queued_thread
            calls += 1
            if calls == 2:
                os.replace(replacement, self.path)
                queued_thread = threading.Thread(target=write_queued)
                queued_thread.start()
                self.assertTrue(queued_started.wait(timeout=5))
            real_fsync(descriptor)

        with patch(
            "agent_container.family_state.os.fsync", side_effect=swap_and_queue_writer
        ):
            with self.assertRaises(ValueError):
                write_family_binding(self.path, self.binding)

        self.assertIsNotNone(queued_thread)
        queued_thread.join(timeout=5)
        self.assertFalse(queued_thread.is_alive())
        self.assertEqual(queued_failures, [])
        self.assertEqual(load_family_binding(self.path), queued)

    def test_write_rejects_binding_replaced_during_observed_update(self) -> None:
        write_family_binding(self.path, FamilyBinding(Repository("family", "old"), 1))
        real_stat = os.stat
        calls = 0

        def changed_stat(*args: object, **kwargs: object) -> os.stat_result:
            nonlocal calls
            calls += 1
            result = real_stat(*args, **kwargs)
            if calls >= 3 and args[0] == self.path.name:
                return _with_inode(result, result.st_ino + 1)
            return result

        with patch("agent_container.family_state.os.stat", side_effect=changed_stat):
            with self.assertRaises(ValueError):
                write_family_binding(self.path, self.binding)

    def test_write_preserves_unrelated_siblings(self) -> None:
        sibling = self.parent / "unrelated.json"
        sibling.write_text("keep", encoding="utf-8")
        sibling.chmod(0o600)

        write_family_binding(self.path, self.binding)

        self.assertEqual(sibling.read_text(encoding="utf-8"), "keep")


def _with_inode(result: os.stat_result, inode: int) -> os.stat_result:
    fields = list(result)
    fields[stat.ST_INO] = inode
    return os.stat_result(fields)


def _with_uid(result: os.stat_result, uid: int) -> os.stat_result:
    fields = list(result)
    fields[stat.ST_UID] = uid
    return os.stat_result(fields)
