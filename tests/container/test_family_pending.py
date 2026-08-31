import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

import agent_container.family_pending as family_pending
from agent_container.family_issue import CanonicalFamilyIssue
from agent_container.family_issue import canonicalize_family_issue
from agent_container.family_issue import FamilyIssueDraft
from agent_container.family_pending import append_family_audit
from agent_container.family_pending import create_pending
from agent_container.family_pending import expire_pending
from agent_container.family_pending import list_pending
from agent_container.family_pending import load_pending
from agent_container.family_pending import pending_lock
from agent_container.family_pending import PendingState
from agent_container.family_pending import recover_sending
from agent_container.family_pending import transition_pending


NOW = 1_800_000_000


class PendingStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = self.root / "family" / "projects" / "demo" / "pending"
        self.audit = self.root / "family" / "projects" / "demo" / "audit" / "events.jsonl"
        for directory in (
            self.root / "family",
            self.root / "family" / "projects",
            self.root / "family" / "projects" / "demo",
            self.store,
            self.audit.parent,
        ):
            directory.mkdir(exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        self.issue = CanonicalFamilyIssue(
            "Add export",
            "## Summary\n\nPortable copy.\n\n## Context\n\nNo export.\n\n"
            "## Acceptance criteria\n\n- JSON downloads\n",
        )

    def _create(self, byte: int = 0x11, *, now: int = NOW):
        return create_pending(
            self.store,
            "demo",
            self.issue,
            now=now,
            random_bytes=lambda size: bytes([byte]) * size,
        )

    def _record_path(self, request_id: str) -> Path:
        return self.store / f"{request_id}.json"

    def _replace_cleanup_name(self, leaked_name: str):
        real_unlink_owned = family_pending._unlink_owned
        replaced = False

        def replace(
            parent_descriptor: int,
            name: str,
            expected: os.stat_result,
        ) -> None:
            nonlocal replaced
            if not replaced and ".json." in name and not name.endswith(".lock"):
                replaced = True
                os.rename(
                    name,
                    leaked_name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=parent_descriptor,
                )
                try:
                    os.write(descriptor, b"replacement")
                finally:
                    os.close(descriptor)
            real_unlink_owned(parent_descriptor, name, expected)

        return replace

    def _transition(
        self,
        request_id: str,
        target: PendingState,
        *,
        issue_number: int | None = None,
        issue_url: str | None = None,
    ):
        with pending_lock(self.store, request_id, "demo") as locked:
            return transition_pending(
                locked,
                target,
                issue_number=issue_number,
                issue_url=issue_url,
            )

    # Break caught: short, non-random, or non-hex request IDs and wrong TTL.
    def test_creates_exact_private_record_with_128_bit_id_and_24_hour_expiry(self) -> None:
        request = self._create()

        self.assertEqual(request.request_id, "11" * 16)
        self.assertEqual(request.project_id, "demo")
        self.assertEqual(request.created_at, NOW)
        self.assertEqual(request.expires_at, 1_800_086_400)
        self.assertEqual(request.state, PendingState.PENDING)
        self.assertEqual(request.issue, self.issue)
        self.assertEqual(self._record_path(request.request_id).stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            json.loads(self._record_path(request.request_id).read_text(encoding="ascii")),
            {
                "body": self.issue.body,
                "created_at": 1_800_000_000,
                "expires_at": 1_800_086_400,
                "project_id": "demo",
                "request_id": "11" * 16,
                "state": "pending",
                "title": "Add export",
            },
        )

    # Break caught: persistence caps rejecting canonical output accepted at every Task 2 limit.
    def test_round_trips_maximum_escaped_canonical_content(self) -> None:
        issue = canonicalize_family_issue(
            FamilyIssueDraft(
                "T" * 256,
                "\\" * 2048,
                "\\" * 4096,
                tuple("\\" * 512 for _ in range(20)),
            )
        )

        request = create_pending(
            self.store,
            "demo",
            issue,
            now=NOW,
            random_bytes=lambda size: b"\x44" * size,
        )

        self.assertEqual(load_pending(self.store, request.request_id, "demo").issue, issue)

    # Break caught: a colliding ID overwriting an existing request.
    def test_retries_id_collision_without_mutating_the_first_record(self) -> None:
        first = self._create()
        values = iter((b"\x11" * 16, b"\x22" * 16))

        second = create_pending(
            self.store,
            "demo",
            self.issue,
            now=NOW + 1,
            random_bytes=lambda size: next(values),
        )

        self.assertEqual(first.request_id, "11" * 16)
        self.assertEqual(second.request_id, "22" * 16)
        self.assertEqual(load_pending(self.store, first.request_id, "demo").created_at, NOW)

    # Break caught: unlimited unfinished requests exhausting host storage/review capacity.
    def test_limits_unfinished_inventory_to_ten_but_ignores_terminal_records(self) -> None:
        requests = [self._create(byte) for byte in range(1, 11)]
        with self.assertRaises(ValueError):
            self._create(11)

        self._transition(requests[0].request_id, PendingState.REJECTED)
        replacement = self._create(11)

        self.assertEqual(replacement.request_id, "0b" * 16)
        self.assertEqual(len(list_pending(self.store, "demo")), 11)

    # Break caught: lifecycle operations mutating accepted title/body bytes.
    def test_nonterminal_transitions_preserve_immutable_canonical_content(self) -> None:
        request = self._create()

        sending = self._transition(request.request_id, PendingState.SENDING)
        pending = self._transition(request.request_id, PendingState.PENDING)

        self.assertEqual(sending.issue, self.issue)
        self.assertEqual(pending.issue, self.issue)
        with self.assertRaisesRegex(Exception, "cannot assign"):
            pending.issue.title = "changed"  # type: ignore[misc,union-attr]

    # Break caught: direct or accidental lifecycle edges bypassing approval/reconciliation.
    def test_accepts_only_the_declared_state_machine_edges(self) -> None:
        allowed = {
            PendingState.PENDING: {
                PendingState.SENDING,
                PendingState.REJECTED,
                PendingState.EXPIRED,
            },
            PendingState.SENDING: {
                PendingState.PENDING,
                PendingState.CREATED,
                PendingState.UNKNOWN,
            },
            PendingState.UNKNOWN: {
                PendingState.PENDING,
                PendingState.CREATED,
            },
            PendingState.CREATED: set(),
            PendingState.REJECTED: set(),
            PendingState.EXPIRED: set(),
        }
        for source in PendingState:
            for target in PendingState:
                with self.subTest(source=source.value, target=target.value):
                    request = self._create(byte=source.value.encode("ascii")[0])
                    if source is not PendingState.PENDING:
                        if source is PendingState.UNKNOWN:
                            self._transition(request.request_id, PendingState.SENDING)
                            self._transition(request.request_id, PendingState.UNKNOWN)
                        elif source is PendingState.CREATED:
                            self._transition(request.request_id, PendingState.SENDING)
                            self._transition(
                                request.request_id,
                                PendingState.CREATED,
                                issue_number=41,
                                issue_url="https://github.com/family/roadmap/issues/41",
                            )
                        else:
                            self._transition(request.request_id, source)
                    kwargs = {}
                    if target is PendingState.CREATED:
                        kwargs = {
                            "issue_number": 42,
                            "issue_url": "https://github.com/family/roadmap/issues/42",
                        }
                    if target in allowed[source]:
                        changed = self._transition(request.request_id, target, **kwargs)
                        self.assertEqual(changed.state, target)
                    else:
                        with self.assertRaises(ValueError):
                            self._transition(request.request_id, target, **kwargs)
                    for path in self.store.iterdir():
                        path.unlink()

    # Break caught: approve, reject, and expiry all winning a stale read concurrently.
    def test_concurrent_approve_reject_and_expiry_serialize_to_one_winner(self) -> None:
        request = self._create()
        barrier = threading.Barrier(3)
        results: list[str] = []

        def attempt(target: PendingState) -> None:
            barrier.wait()
            try:
                if target is PendingState.EXPIRED:
                    expire_pending(self.store, request.request_id, "demo", now=request.expires_at)
                else:
                    self._transition(request.request_id, target)
            except ValueError:
                results.append("lost")
            else:
                results.append(target.value)

        threads = [
            threading.Thread(target=attempt, args=(target,))
            for target in (
                PendingState.SENDING,
                PendingState.REJECTED,
                PendingState.EXPIRED,
            )
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(results.count("lost"), 2)
        self.assertIn(load_pending(self.store, request.request_id, "demo").state.value, results)

    # Break caught: two approvals both crossing pending -> sending.
    def test_double_approve_has_exactly_one_successful_transition(self) -> None:
        request = self._create()
        barrier = threading.Barrier(2)
        outcomes: list[bool] = []

        def approve() -> None:
            barrier.wait()
            try:
                self._transition(request.request_id, PendingState.SENDING)
            except ValueError:
                outcomes.append(False)
            else:
                outcomes.append(True)

        threads = [threading.Thread(target=approve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(sorted(outcomes), [False, True])

    # Break caught: approval releasing its lock between sending and the remote result.
    def test_one_lock_handle_spans_sending_remote_work_and_final_transition(self) -> None:
        request = self._create()
        competitor_started = threading.Event()
        competitor_acquired = threading.Event()
        competitor_done = threading.Event()

        def reject() -> None:
            competitor_started.set()
            try:
                with pending_lock(self.store, request.request_id, "demo") as locked:
                    competitor_acquired.set()
                    transition_pending(locked, PendingState.REJECTED)
            except ValueError:
                pass
            finally:
                competitor_done.set()

        with pending_lock(self.store, request.request_id, "demo") as locked:
            transition_pending(locked, PendingState.SENDING)
            thread = threading.Thread(target=reject)
            thread.start()
            self.assertTrue(competitor_started.wait(1))
            self.assertFalse(competitor_acquired.wait(0.05))
            transition_pending(locked, PendingState.UNKNOWN)

        self.assertTrue(competitor_done.wait(1))
        thread.join(timeout=1)
        self.assertEqual(load_pending(self.store, request.request_id, "demo").state, PendingState.UNKNOWN)

    # Break caught: restart treating an in-flight send as safely retryable.
    def test_recovery_moves_only_surviving_sending_to_unknown(self) -> None:
        request = self._create()
        self._transition(request.request_id, PendingState.SENDING)

        recovered = recover_sending(self.store, request.request_id, "demo")

        self.assertEqual(recovered.state, PendingState.UNKNOWN)
        self.assertEqual(recovered.issue, self.issue)
        with self.assertRaises(ValueError):
            recover_sending(self.store, request.request_id, "demo")

    # Break caught: restart inventory leaving an interrupted send retryable by omission.
    def test_store_initialization_recovers_every_sending_record_without_id(self) -> None:
        pending = self._create(1)
        sending = self._create(2)
        self._transition(sending.request_id, PendingState.SENDING)

        before = {request.request_id: request.state for request in list_pending(self.store, "demo")}
        self.assertEqual(before[pending.request_id], PendingState.PENDING)
        self.assertEqual(before[sending.request_id], PendingState.SENDING)

        initialized = family_pending.initialize_pending_store(self.store, "demo")

        self.assertEqual(
            [(request.request_id, request.project_id) for request in initialized],
            [(sending.request_id, "demo")],
        )
        self.assertEqual(
            load_pending(self.store, pending.request_id, "demo").state,
            PendingState.PENDING,
        )
        self.assertEqual(load_pending(self.store, sending.request_id, "demo").state, PendingState.UNKNOWN)

    # Break caught: expiry before its exact deadline or from an in-flight state.
    def test_expiry_requires_pending_state_at_or_after_deadline(self) -> None:
        request = self._create()
        with self.assertRaises(ValueError):
            expire_pending(self.store, request.request_id, "demo", now=request.expires_at - 1)

        expired = expire_pending(self.store, request.request_id, "demo", now=request.expires_at)

        self.assertEqual(expired.state, PendingState.EXPIRED)

    # Break caught: sensitive canonical content surviving terminal cleanup.
    def test_terminal_records_delete_title_and_body_and_keep_only_allowed_result(self) -> None:
        for byte, terminal in ((1, PendingState.REJECTED), (2, PendingState.EXPIRED)):
            request = self._create(byte)
            if terminal is PendingState.EXPIRED:
                changed = expire_pending(self.store, request.request_id, "demo", now=request.expires_at)
            else:
                changed = self._transition(request.request_id, terminal)
            payload = json.loads(self._record_path(request.request_id).read_text("ascii"))
            self.assertIsNone(changed.issue)
            self.assertEqual(
                set(payload),
                {"created_at", "expires_at", "project_id", "request_id", "state"},
            )

        request = self._create(3)
        self._transition(request.request_id, PendingState.SENDING)
        created = self._transition(
            request.request_id,
            PendingState.CREATED,
            issue_number=42,
            issue_url="https://github.com/family/roadmap/issues/42",
        )
        payload = json.loads(self._record_path(request.request_id).read_text("ascii"))
        self.assertIsNone(created.issue)
        self.assertEqual(created.issue_number, 42)
        self.assertEqual(
            set(payload),
            {
                "created_at",
                "expires_at",
                "issue_number",
                "issue_url",
                "project_id",
                "request_id",
                "state",
            },
        )
        self.assertNotIn("Add export", self._record_path(request.request_id).read_text("ascii"))
        self.assertNotIn("Portable copy", self._record_path(request.request_id).read_text("ascii"))

    # Break caught: an attacker-controlled or crash-left sibling being ignored during inventory.
    def test_unknown_sibling_causes_inventory_to_fail_closed(self) -> None:
        self._create()
        unknown = self.store / "attacker"
        unknown.write_text("content", encoding="ascii")
        unknown.chmod(0o600)

        with self.assertRaises(ValueError):
            list_pending(self.store, "demo")
        with self.assertRaises(ValueError):
            self._create(2)

    # Break caught: a lookup for a missing ID leaving an orphan lock that poisons inventory.
    def test_missing_request_lookup_does_not_create_any_sibling(self) -> None:
        request = self._create()
        before = {path.name for path in self.store.iterdir()}

        with self.assertRaises(ValueError):
            load_pending(self.store, "ff" * 16, "demo")

        self.assertEqual({path.name for path in self.store.iterdir()}, before)
        self.assertEqual(list_pending(self.store, "demo"), (request,))

    # Break caught: a valid-looking sibling with unsafe metadata being ignored by point loads.
    def test_point_load_fails_closed_on_an_unsafe_record_sibling(self) -> None:
        request = self._create()
        unsafe = self._record_path("ee" * 16)
        unsafe.write_text("{}\n", encoding="ascii")
        unsafe.chmod(0o644)

        with self.assertRaises(PermissionError):
            load_pending(self.store, request.request_id, "demo")

    # Break caught: crashes before publication changing the durable state.
    def test_write_fsync_rename_and_prepublication_parent_fsync_failures_preserve_state(self) -> None:
        request = self._create()
        real_fsync = os.fsync

        cases = []
        cases.append(patch("agent_container.family_pending._write_all", side_effect=OSError("write")))

        def fail_file_fsync(descriptor: int) -> None:
            if os.path.isfile(f"/proc/self/fd/{descriptor}"):
                raise OSError("file fsync")
            real_fsync(descriptor)

        cases.append(patch("agent_container.family_pending.os.fsync", side_effect=fail_file_fsync))
        cases.append(patch("agent_container.family_pending._rename_exchange", side_effect=OSError("rename")))

        def fail_directory_fsync(descriptor: int) -> None:
            if os.path.isdir(f"/proc/self/fd/{descriptor}"):
                raise OSError("parent fsync")
            real_fsync(descriptor)

        cases.append(patch("agent_container.family_pending.os.fsync", side_effect=fail_directory_fsync))

        for failure in cases:
            with self.subTest(failure=failure.attribute):
                with failure:
                    with self.assertRaises(OSError):
                        self._transition(request.request_id, PendingState.SENDING)
                self.assertEqual(load_pending(self.store, request.request_id, "demo").state, PendingState.PENDING)

    # Break caught: lock failure allowing an unlocked state update.
    def test_lock_acquisition_failure_leaves_record_unchanged(self) -> None:
        request = self._create()

        with patch("agent_container.family_pending.fcntl.flock", side_effect=OSError("lock")):
            with self.assertRaises(OSError):
                self._transition(request.request_id, PendingState.SENDING)

        self.assertEqual(load_pending(self.store, request.request_id, "demo").state, PendingState.PENDING)

    # Break caught: an exception from the atomic state-update layer being ignored.
    def test_state_update_failure_never_reaches_a_success_audit(self) -> None:
        request = self._create()

        with patch("agent_container.family_pending._atomic_replace", side_effect=OSError("state")):
            with self.assertRaises(OSError):
                self._transition(request.request_id, PendingState.REJECTED)

        self.assertEqual(load_pending(self.store, request.request_id, "demo").state, PendingState.PENDING)
        self.assertFalse(self.audit.exists())

    # Break caught: a post-publication directory-sync failure being reported as no state change.
    def test_final_parent_fsync_failure_reports_the_new_published_state(self) -> None:
        request = self._create()
        real_fsync = os.fsync
        directory_syncs = 0

        def fail_second_directory_fsync(descriptor: int) -> None:
            nonlocal directory_syncs
            if os.path.isdir(f"/proc/self/fd/{descriptor}"):
                directory_syncs += 1
                if directory_syncs == 2:
                    raise OSError("final parent fsync")
            real_fsync(descriptor)

        with patch("agent_container.family_pending.os.fsync", side_effect=fail_second_directory_fsync):
            with self.assertRaises(OSError):
                self._transition(request.request_id, PendingState.SENDING)

        self.assertEqual(load_pending(self.store, request.request_id, "demo").state, PendingState.SENDING)
        self.assertFalse(self.audit.exists())

    # Break caught: cleanup failure being followed by a success audit event.
    def test_content_cleanup_failure_never_emits_success_event(self) -> None:
        request = self._create()
        self._transition(request.request_id, PendingState.SENDING)

        def transition_then_audit() -> None:
            self._transition(
                request.request_id,
                PendingState.CREATED,
                issue_number=42,
                issue_url="https://github.com/family/roadmap/issues/42",
            )
            append_family_audit(
                self.audit,
                timestamp=NOW,
                project_id="demo",
                request_id=request.request_id,
                operation="approve",
                status="created",
                stage="cleanup",
            )

        with patch("agent_container.family_pending._unlink_owned", side_effect=OSError("unlink")):
            with self.assertRaises(OSError):
                transition_then_audit()

        self.assertFalse(self.audit.exists())
        self.assertEqual(
            json.loads(self._record_path(request.request_id).read_text("ascii"))["state"],
            "created",
        )
        with self.assertRaises(ValueError):
            load_pending(self.store, request.request_id, "demo")
        self.assertTrue(any("Add export" in path.read_text("ascii") for path in self.store.glob(".*.json.*")))

    # Break caught: name-based cleanup silently succeeding after the displaced inode is moved.
    def test_terminal_transition_rejects_moved_and_replaced_cleanup_name(self) -> None:
        request = self._create()
        leaked_name = "leaked-sensitive-content"

        with patch(
            "agent_container.family_pending._unlink_owned",
            side_effect=self._replace_cleanup_name(leaked_name),
        ):
            with self.assertRaises(ValueError):
                self._transition(request.request_id, PendingState.REJECTED)

        self.assertIn("Add export", (self.store / leaked_name).read_text("ascii"))
        with self.assertRaises(ValueError):
            list_pending(self.store, "demo")

    # Break caught: new-record publication accepting an extra sensitive hard link.
    def test_new_record_rejects_moved_and_replaced_cleanup_name(self) -> None:
        leaked_name = "leaked-new-record"

        with patch(
            "agent_container.family_pending._unlink_owned",
            side_effect=self._replace_cleanup_name(leaked_name),
        ):
            with self.assertRaises(ValueError):
                self._create()

        self.assertIn("Add export", (self.store / leaked_name).read_text("ascii"))
        with self.assertRaises(ValueError):
            list_pending(self.store, "demo")

    # Break caught: an extra content link appearing during the final parent sync.
    def test_new_record_revalidates_cleanup_after_parent_fsync(self) -> None:
        real_fsync = os.fsync
        linked = False

        def link_during_parent_sync(descriptor: int) -> None:
            nonlocal linked
            if not linked and os.path.isdir(f"/proc/self/fd/{descriptor}"):
                linked = True
                os.link(
                    "11" * 16 + ".json",
                    "late-sensitive-link",
                    src_dir_fd=descriptor,
                    dst_dir_fd=descriptor,
                    follow_symlinks=False,
                )
            real_fsync(descriptor)

        with patch(
            "agent_container.family_pending.os.fsync",
            side_effect=link_during_parent_sync,
        ):
            with self.assertRaises(ValueError):
                self._create()

        self.assertIn(
            "Add export",
            (self.store / "late-sensitive-link").read_text("ascii"),
        )

    # Break caught: a layout opening a request whose embedded project differs.
    def test_pending_lock_requires_and_validates_expected_project_before_yield(self) -> None:
        request = self._create()
        yielded = False
        try:
            with pending_lock(
                self.store,
                request.request_id,
                expected_project_id="other",
            ):
                yielded = True
        except TypeError:
            self.fail("pending_lock does not accept required expected_project_id")
        except ValueError:
            pass
        self.assertFalse(yielded)

    # Break caught: value equality accepting a replacement inode after preview.
    def test_pending_snapshot_rejects_equal_bytes_on_a_new_inode(self) -> None:
        snapshotter = getattr(family_pending, "snapshot_pending", None)
        self.assertTrue(callable(snapshotter))
        request = self._create()
        snapshot = snapshotter(self.store, request.request_id, "demo")
        record = self._record_path(request.request_id)
        replacement = self.store / ".equal-replacement"
        replacement.write_bytes(record.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, record)

        with self.assertRaises(ValueError):
            with pending_lock(
                self.store,
                request.request_id,
                "demo",
                snapshot=snapshot,
            ):
                self.fail("replacement inode was exposed")

    # Break caught: startup returning all sensitive requests instead of recovered IDs.
    def test_initializer_returns_only_safe_metadata_for_recovered_sends(self) -> None:
        pending = self._create(1)
        sending = self._create(2)
        self._transition(sending.request_id, PendingState.SENDING)
        try:
            recovered = family_pending.initialize_pending_store(
                self.store, "demo"
            )
        except TypeError:
            self.fail("initializer does not accept expected project")

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].request_id, sending.request_id)
        self.assertEqual(recovered[0].project_id, "demo")
        self.assertFalse(hasattr(recovered[0], "issue"))
        self.assertEqual(
            load_pending(self.store, pending.request_id, "demo").state,
            PendingState.PENDING,
        )
        self.assertEqual(
            load_pending(self.store, sending.request_id, "demo").state,
            PendingState.UNKNOWN,
        )


class FamilyAuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.parent = self.root / "audit"
        self.parent.mkdir(mode=0o700)
        self.path = self.parent / "events.jsonl"
        self.request_id = "ab" * 16

    def _append(self, **changes: object) -> None:
        values = {
            "timestamp": NOW,
            "project_id": "demo",
            "request_id": self.request_id,
            "operation": "approve",
            "status": "created",
            "stage": "cleanup",
        }
        values.update(changes)
        append_family_audit(self.path, **values)  # type: ignore[arg-type]

    # Break caught: audit records gaining request content or non-exact fields.
    def test_appends_one_exact_ascii_content_free_json_line_to_private_file(self) -> None:
        self._append()

        self.assertEqual(
            self.path.read_bytes(),
            (
                '{"operation":"approve","project_id":"demo","request_id":"'
                + self.request_id
                + '","stage":"cleanup","status":"created","timestamp":1800000000}\n'
            ).encode("ascii"),
        )
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    # Break caught: free-form audit values smuggling content, repository, URL, or errors.
    def test_rejects_every_value_outside_closed_vocabularies_and_exact_types(self) -> None:
        cases = (
            {"operation": "Add export"},
            {"operation": "https://github.com/family/roadmap"},
            {"status": "Portable copy"},
            {"status": "RuntimeError: token leaked"},
            {"stage": "repository"},
            {"stage": "response body"},
            {"project_id": "family/roadmap"},
            {"request_id": "not-an-id"},
            {"timestamp": True},
        )
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    self._append(**values)
        self.assertFalse(self.path.exists())

    # Break caught: concurrent audit writers interleaving partial JSON lines.
    def test_concurrent_appends_are_locked_complete_json_lines(self) -> None:
        threads = [
            threading.Thread(
                target=self._append,
                kwargs={"request_id": f"{number:032x}", "timestamp": NOW + number},
            )
            for number in range(12)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        lines = self.path.read_text("ascii").splitlines()
        self.assertEqual(len(lines), 12)
        self.assertEqual(
            {json.loads(line)["request_id"] for line in lines},
            {f"{number:032x}" for number in range(12)},
        )

    # Break caught: a failed audit append being mistaken for a durable event.
    def test_append_write_and_fsync_failures_are_reported_without_success_claim(self) -> None:
        with patch("agent_container.family_pending._write_all", side_effect=OSError("append")):
            with self.assertRaises(OSError):
                self._append()

        if self.path.exists():
            self.assertEqual(self.path.read_bytes(), b"")

        real_fsync = os.fsync

        def fail_file_fsync(descriptor: int) -> None:
            if os.path.isfile(f"/proc/self/fd/{descriptor}"):
                raise OSError("audit fsync")
            real_fsync(descriptor)

        with patch("agent_container.family_pending.os.fsync", side_effect=fail_file_fsync):
            with self.assertRaises(OSError):
                self._append(request_id="cd" * 16)

    # Break caught: a short audit write poisoning every later JSONL consumer.
    def test_partial_audit_write_rolls_back_before_a_later_safe_append(self) -> None:
        self._append()
        before = self.path.read_bytes()

        def write_partial_then_fail(descriptor: int, body: bytes) -> None:
            os.write(descriptor, body[:17])
            raise OSError("partial audit append")

        with patch(
            "agent_container.family_pending._write_all",
            side_effect=write_partial_then_fail,
        ):
            with self.assertRaises(OSError):
                self._append(request_id="cd" * 16)

        self.assertEqual(self.path.read_bytes(), before)
        self._append(request_id="ef" * 16, status="error", stage="send")
        lines = self.path.read_text("ascii").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[1])["request_id"], "ef" * 16)

    # Break caught: append treating a malformed or unterminated existing tail as valid JSONL.
    def test_rejects_malformed_existing_audit_tail_without_appending(self) -> None:
        existing = (
            '{"operation":"approve","project_id":"demo","request_id":"'
            + self.request_id
            + '","stage":"cleanup","status":"created","timestamp":1800000000}\n'
            + '{"operation":"approve"'
        ).encode("ascii")
        self.path.write_bytes(existing)
        self.path.chmod(0o600)

        with self.assertRaises(ValueError):
            self._append(request_id="cd" * 16)

        self.assertEqual(self.path.read_bytes(), existing)

    # Break caught: a concurrent opener reporting a new audit entry before its directory entry is durable.
    def test_every_append_syncs_the_parent_even_when_the_audit_file_exists(self) -> None:
        self._append()
        real_fsync = os.fsync
        parent_syncs = 0

        def record_fsync(descriptor: int) -> None:
            nonlocal parent_syncs
            if os.path.isdir(f"/proc/self/fd/{descriptor}"):
                parent_syncs += 1
            real_fsync(descriptor)

        with patch("agent_container.family_pending.os.fsync", side_effect=record_fsync):
            self._append(request_id="cd" * 16)

        self.assertEqual(parent_syncs, 1)

    # Break caught: an audit append returning after its flocked inode leaves the canonical path.
    def test_rejects_canonical_audit_path_swap_after_file_or_parent_fsync(self) -> None:
        real_fsync = os.fsync
        for phase in ("file", "parent"):
            with self.subTest(phase=phase):
                displaced = self.parent / f"events.{phase}.old"
                for path in (self.path, displaced):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                swapped = False

                def swap_after_sync(descriptor: int) -> None:
                    nonlocal swapped
                    is_parent = os.path.isdir(f"/proc/self/fd/{descriptor}")
                    real_fsync(descriptor)
                    if not swapped and (
                        (phase == "file" and not is_parent)
                        or (phase == "parent" and is_parent)
                    ):
                        swapped = True
                        self.path.rename(displaced)
                        self.path.write_bytes(b"")
                        self.path.chmod(0o600)

                with patch(
                    "agent_container.family_pending.os.fsync",
                    side_effect=swap_after_sync,
                ):
                    with self.assertRaises(ValueError):
                        self._append()

                self.assertEqual(self.path.read_bytes(), b"")
                self.assertIn(b'"status":"created"', displaced.read_bytes())

    # Break caught: doctor relying on the append path and creating or trusting bad audit.
    def test_read_only_audit_validator_is_bounded_exact_and_project_scoped(self) -> None:
        validator = getattr(family_pending, "validate_family_audit", None)
        self.assertTrue(callable(validator))
        self.assertEqual(validator(self.path, "demo"), 0)
        self.assertFalse(self.path.exists())

        self._append()
        self.assertEqual(validator(self.path, "demo"), 1)
        with self.assertRaises(ValueError):
            validator(self.path, "other")

        self.path.write_bytes(b"{not-json}\n")
        self.path.chmod(0o600)
        with self.assertRaises(ValueError):
            validator(self.path, "demo")

        self.path.chmod(0o644)
        with self.assertRaises((PermissionError, ValueError)):
            validator(self.path, "demo")


if __name__ == "__main__":
    unittest.main()
