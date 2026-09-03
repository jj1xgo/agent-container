from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from agent_container.handover_broker_protocol import MAX_DOCUMENT_BYTES
from agent_container.handover_writer import create_atomic_handover
from agent_container.handover_writer import render_handover
from agent_container.handover_writer import validate_handover_content


REQUIRED_HEADINGS = (
    "## 作業の目的",
    "## 現在地",
    "## 決定事項と理由",
    "## 変更したファイル・commit・PR",
    "## 検証結果",
    "## 未解決事項とリスク",
    "## 次の一手",
)


def valid_body() -> str:
    return "\n\n".join(f"{heading}\ncontent" for heading in REQUIRED_HEADINGS) + "\n"


class HandoverValidationTest(unittest.TestCase):
    def test_accepts_exact_sections_and_lower_level_headings(self) -> None:
        title, body = validate_handover_content(
            " Safe title ", valid_body().replace("content", "### Detail\ncontent", 1)
        )
        self.assertEqual(title, "Safe title")
        self.assertTrue(body.endswith("\n"))

    def test_rejects_invalid_titles_and_bodies(self) -> None:
        missing = "\n\n".join(f"{heading}\ncontent" for heading in REQUIRED_HEADINGS[1:])
        duplicate = valid_body().replace(
            "## 現在地", "## 作業の目的", 1
        )
        reordered = "\n\n".join(
            f"{heading}\ncontent" for heading in reversed(REQUIRED_HEADINGS)
        )
        unknown = valid_body() + "\n## 余分な見出し\ncontent\n"
        oversized = "あ" * ((MAX_DOCUMENT_BYTES // len("あ".encode())) + 1)
        cases = (
            ("", valid_body()),
            ("\nnot one line", valid_body()),
            ("safe", missing),
            ("safe", duplicate),
            ("safe", reordered),
            ("safe", unknown),
            ("safe", "# Handover\n\n" + valid_body()),
            ("safe", "- Created: bad\n\n" + valid_body()),
            ("safe", valid_body() + "\x00"),
            ("safe", oversized),
            ("safe", valid_body() + "\nghp_abcdefghijklmnopqrst\n"),
            ("safe", valid_body() + "\ngithub_pat_abcdefghijklmnopqrst\n"),
            ("safe", valid_body() + "\n(sk-abcdefghijklmnop)\n"),
            ("safe", valid_body() + "\n-----BEGIN OPENSSH PRIVATE KEY-----\n"),
        )
        for title, body in cases:
            with self.subTest(title=title, body=body[:40]), self.assertRaises(ValueError):
                validate_handover_content(title, body)

    def test_accepts_risk_based_text(self) -> None:
        _, body = validate_handover_content(
            "safe", valid_body().replace("content", "risk-based decision", 1)
        )
        self.assertIn("risk-based", body)

    def test_rejects_normalized_over_limit_and_surrogate_text(self) -> None:
        exact_limit_without_newline = valid_body()
        exact_limit_without_newline = exact_limit_without_newline[:-1] + "x" * (
            MAX_DOCUMENT_BYTES - len(exact_limit_without_newline[:-1].encode("utf-8"))
        )
        cases = (
            ("title", exact_limit_without_newline),
            ("bad\ud800title", valid_body()),
            ("title", valid_body() + "\ud800"),
        )
        for title, body in cases:
            with self.subTest(title=title), self.assertRaises(ValueError):
                validate_handover_content(title, body)


class AtomicHandoverWriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.now = datetime(2026, 8, 27, 12, 34, 56, tzinfo=timezone(timedelta(hours=9)))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_publishes_expected_utc_filename_mode_and_canonical_bytes(self) -> None:
        created = create_atomic_handover(
            self.project,
            "project",
            " Safe title ",
            valid_body(),
            now=self.now,
            token_hex=lambda size: "a" * (size * 2),
        )
        expected_now = datetime(2026, 8, 27, 3, 34, 56, tzinfo=timezone.utc)
        self.assertEqual(created.name, "2026-08-27_033456_aaaaaaaa.md")
        self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o600)
        self.assertEqual(
            created.read_bytes(), render_handover("project", "Safe title", valid_body(), expected_now)
        )
        self.assertEqual(list(self.project.glob(".handover-*.tmp")), [])

    def test_records_a_trusted_host_session_id(self) -> None:
        created = create_atomic_handover(
            self.project,
            "project",
            "title",
            valid_body(),
            self.now,
            lambda size: "a" * (size * 2),
            session_id="session-123",
        )

        self.assertIn(
            "- Session: session-123\n",
            created.read_text(encoding="utf-8"),
        )

    def test_forces_mode_0600_despite_a_restrictive_umask(self) -> None:
        original_umask = os.umask(0o777)
        try:
            created = create_atomic_handover(
                self.project,
                "project",
                "title",
                valid_body(),
                self.now,
                lambda size: "a" * (size * 2),
            )
        finally:
            os.umask(original_umask)
        self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o600)

    def test_rejects_a_rendered_document_larger_than_the_protocol_limit(self) -> None:
        body = valid_body()
        body = body[:-1] + "x" * (MAX_DOCUMENT_BYTES - len(body.encode("utf-8")))
        with self.assertRaises(ValueError):
            render_handover("project", "title", body, self.now)

    def test_retries_only_final_name_collisions_without_replacing_existing_file(self) -> None:
        collision = self.project / "2026-08-27_033456_deadbeef.md"
        collision.write_bytes(b"keep me")
        tokens = iter(("a" * 16, "deadbeef", "b" * 16, "cafebabe"))
        created = create_atomic_handover(
            self.project, "project", "title", valid_body(), self.now, lambda _: next(tokens)
        )
        self.assertEqual(collision.read_bytes(), b"keep me")
        self.assertEqual(created.name, "2026-08-27_033456_cafebabe.md")
        self.assertEqual(list(self.project.glob(".handover-*.tmp")), [])

    def test_rejects_symlinked_project_and_mismatched_project_id(self) -> None:
        target = Path(self.temp.name) / "target"
        target.mkdir()
        linked = Path(self.temp.name) / "project-link"
        linked.symlink_to(target, target_is_directory=True)
        for directory, project_id in ((linked, "project-link"), (self.project, "other")):
            with self.subTest(directory=directory, project_id=project_id), self.assertRaises(ValueError):
                create_atomic_handover(directory, project_id, "title", valid_body(), self.now)
        self.assertEqual(list(self.project.glob("*.md")), [])
        self.assertEqual(list(target.glob("*.md")), [])

    def test_rejects_relative_traversal_and_symlinked_ancestor_project_paths(self) -> None:
        original_cwd = Path.cwd()
        ancestor = Path(self.temp.name) / "ancestor"
        ancestor.symlink_to(Path(self.temp.name), target_is_directory=True)
        relative = Path("project")
        traversal = Path(".") / ".." / Path(self.temp.name).name / "project"
        try:
            os.chdir(self.temp.name)
            cases = (relative, traversal, ancestor / "project")
            for directory in cases:
                with self.subTest(directory=directory), self.assertRaises(ValueError):
                    create_atomic_handover(directory, "project", "title", valid_body(), self.now)
        finally:
            os.chdir(original_cwd)

    def test_rejects_a_directory_replacement_after_pinning(self) -> None:
        original_open = os.open
        pinned = Path(self.temp.name) / "pinned"
        replaced = False

        def open_and_replace(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal replaced
            descriptor = original_open(path, flags, *args, **kwargs)
            if not replaced and (path == self.project or path == "project"):
                self.project.rename(pinned)
                self.project.mkdir()
                replaced = True
            return descriptor

        with mock.patch("agent_container.handover_writer.os.open", side_effect=open_and_replace):
            with self.assertRaises(ValueError):
                create_atomic_handover(
                    self.project,
                    "project",
                    "title",
                    valid_body(),
                    self.now,
                    lambda size: "a" * (size * 2),
                )
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertEqual(list(pinned.iterdir()), [])

    def test_rejects_an_ancestor_symlink_replacement_during_validation(self) -> None:
        ancestor = Path(self.temp.name) / "ancestor"
        project = ancestor / "project"
        ancestor.mkdir()
        project.mkdir()
        trusted_ancestor = Path(self.temp.name) / "trusted-ancestor"
        attacker_ancestor = Path(self.temp.name) / "attacker-ancestor"
        original_open = os.open
        original_resolve = Path.resolve
        swapped = False

        def replace_ancestor() -> None:
            nonlocal swapped
            ancestor.rename(trusted_ancestor)
            attacker_ancestor.mkdir()
            (attacker_ancestor / "project").mkdir()
            ancestor.symlink_to(attacker_ancestor, target_is_directory=True)
            swapped = True

        def resolve_and_swap(path: Path, *args: object, **kwargs: object) -> Path:
            resolved = original_resolve(path, *args, **kwargs)
            if not swapped and path == project:
                replace_ancestor()
            return resolved

        def open_and_swap(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal swapped
            if not swapped and path == "ancestor":
                replace_ancestor()
            return original_open(path, flags, *args, **kwargs)

        with mock.patch(
            "agent_container.handover_writer.Path.resolve", autospec=True, side_effect=resolve_and_swap
        ), mock.patch("agent_container.handover_writer.os.open", side_effect=open_and_swap):
            with self.assertRaises(ValueError):
                create_atomic_handover(
                    project,
                    "project",
                    "title",
                    valid_body(),
                    self.now,
                    lambda size: "a" * (size * 2),
                )
        self.assertEqual(list((ancestor / "project").iterdir()), [])
        self.assertEqual(list((trusted_ancestor / "project").iterdir()), [])

    def test_invalid_requests_do_not_leak_project_directory_descriptors(self) -> None:
        fd_root = Path("/proc/self/fd")
        if not fd_root.is_dir():
            self.skipTest("FD counting requires procfs")
        oversized = valid_body()[:-1] + "x" * MAX_DOCUMENT_BYTES
        cases = (
            ("bad\ntitle", valid_body(), self.now),
            ("title", "not a handover", self.now),
            ("title", valid_body(), self.now.replace(tzinfo=None)),
            ("title", oversized, self.now),
        )
        before = len(list(fd_root.iterdir()))
        for title, body, now in cases:
            with self.subTest(title=title), self.assertRaises(ValueError):
                create_atomic_handover(self.project, "project", title, body, now)
        self.assertEqual(len(list(fd_root.iterdir())), before)
        self.assertEqual(list(self.project.iterdir()), [])

    def test_cleans_up_when_writing_linking_or_fsync_fails(self) -> None:
        failures = (
            ("write", "os.write", OSError("write failed")),
            ("link", "os.link", OSError("link failed")),
            ("data fsync", "os.fsync", OSError("fsync failed")),
            ("directory fsync", "os.fsync", [None, OSError("fsync failed")]),
        )
        for name, target, failure in failures:
            with self.subTest(name=name), mock.patch(
                f"agent_container.handover_writer.{target}", side_effect=failure
            ):
                with self.assertRaises(OSError):
                    create_atomic_handover(
                        self.project,
                        "project",
                        "title",
                        valid_body(),
                        self.now,
                        lambda size: "a" * (size * 2),
                    )
            self.assertEqual(list(self.project.iterdir()), [])

    def test_rollback_never_unlinks_a_replacement_at_the_final_name(self) -> None:
        final = self.project / "2026-08-27_033456_aaaaaaaa.md"
        replacement = b"attacker replacement"
        real_fsync = os.fsync

        def replace_final_before_directory_failure(descriptor: int) -> None:
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                final.unlink()
                final.write_bytes(replacement)
                raise OSError("private-directory-fsync-marker")
            real_fsync(descriptor)

        with mock.patch(
            "agent_container.handover_writer.os.fsync",
            side_effect=replace_final_before_directory_failure,
        ):
            with self.assertRaises(OSError) as raised:
                create_atomic_handover(
                    self.project,
                    "project",
                    "title",
                    valid_body(),
                    self.now,
                    lambda size: "a" * (size * 2),
                )

        self.assertEqual(final.read_bytes(), replacement)
        self.assertNotIn("private-directory-fsync-marker", str(raised.exception))
        self.assertEqual(list(self.project.glob(".handover-*.tmp")), [])

    def test_temp_cleanup_failure_still_rolls_back_the_owned_final(self) -> None:
        temporary = ".handover-aaaaaaaaaaaaaaaa.tmp"
        final = "2026-08-27_033456_aaaaaaaa.md"
        real_unlink = os.unlink

        def fail_temporary_cleanup(
            path: object, *args: object, **kwargs: object
        ) -> None:
            if path == temporary:
                raise OSError("private-temp-cleanup-marker")
            real_unlink(path, *args, **kwargs)

        try:
            with mock.patch(
                "agent_container.handover_writer.os.unlink",
                side_effect=fail_temporary_cleanup,
            ):
                with self.assertRaises(OSError) as raised:
                    create_atomic_handover(
                        self.project,
                        "project",
                        "title",
                        valid_body(),
                        self.now,
                        lambda size: "a" * (size * 2),
                    )

            self.assertFalse((self.project / final).exists())
            self.assertTrue((self.project / temporary).exists())
            self.assertNotIn("private-temp-cleanup-marker", str(raised.exception))
        finally:
            try:
                real_unlink(self.project / temporary)
            except FileNotFoundError:
                pass
