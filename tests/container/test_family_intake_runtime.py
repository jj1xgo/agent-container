import ast
import json
import os
from pathlib import Path
import socket
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_container.family_intake_runtime import FamilyIntakeRuntime
from agent_container.family_intake_runtime import FamilyIntakeRuntimeError
from agent_container.family_intake_runtime import FamilyRuntimeMount
from agent_container.family_issue import CanonicalFamilyIssue
from agent_container.family_pending import create_pending
from agent_container.family_pending import load_pending
from agent_container.family_pending import pending_lock
from agent_container.family_pending import PendingState
from agent_container.family_pending import transition_pending
from agent_container.family_state import FamilyBinding
from agent_container.family_state import FamilyStateLayout
from agent_container.family_state import write_family_binding
from agent_container.state import Repository


NOW = 1_800_000_000


class FamilyIntakeRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.layout = FamilyStateLayout(self.root, "demo")
        for directory in (
            self.layout.family_root,
            self.layout.family_root / "projects",
            self.layout.family_project_dir,
            self.layout.family_pending_dir,
            self.layout.family_audit_file.parent,
        ):
            directory.mkdir(exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        write_family_binding(
            self.layout.family_binding_file,
            FamilyBinding(Repository.parse("family/roadmap"), 12345),
        )

    def runtime(self, **changes: object) -> FamilyIntakeRuntime:
        values = {
            "layout": self.layout,
            "peer_pid": os.getpid(),
            "clock": lambda: NOW,
            "random_bytes": lambda size: b"\x33" * size,
        }
        values.update(changes)
        return FamilyIntakeRuntime.create(**values)  # type: ignore[arg-type]

    # Break caught: future Podman wiring needing to reconstruct or leak host runtime values.
    def test_mount_contains_only_socket_directory_capability_and_exact_environment(self) -> None:
        mount = FamilyRuntimeMount(
            Path("/private/run"),
            "c" * 43,
            {
                "AGENT_FAMILY_SOCKET": "/run/agent-family/intake.sock",
                "AGENT_FAMILY_CAPABILITY": "c" * 43,
            },
        )

        self.assertEqual(mount.socket_path, Path("/private/run/intake.sock"))
        self.assertEqual(
            mount.environment,
            {
                "AGENT_FAMILY_SOCKET": "/run/agent-family/intake.sock",
                "AGENT_FAMILY_CAPABILITY": "c" * 43,
            },
        )
        self.assertNotIn("c" * 43, repr(mount))

    # Break caught: accepting clients before startup recovery converts sending to unknown.
    def test_start_recovers_sending_before_listener_is_opened(self) -> None:
        pending = create_pending(
            self.layout.family_pending_dir,
            "demo",
            CanonicalFamilyIssue("Title", "Body"),
            now=NOW,
            random_bytes=lambda size: b"\x44" * size,
        )
        with pending_lock(self.layout.family_pending_dir, pending.request_id) as locked:
            transition_pending(locked, PendingState.SENDING)
        runtime = self.runtime()
        order: list[str] = []
        real_initialize = __import__(
            "agent_container.family_intake_runtime", fromlist=["initialize_pending_store"]
        ).initialize_pending_store
        def initialize(store: Path):
            order.append("initialize")
            return real_initialize(store)

        class RecordingSocket(socket.socket):
            def bind(self, address: str) -> None:
                order.append("bind")
                super().bind(address)

        with patch(
            "agent_container.family_intake_runtime.initialize_pending_store",
            side_effect=initialize,
        ), patch(
            "agent_container.family_intake_runtime.socket.socket",
            side_effect=lambda *args, **kwargs: RecordingSocket(*args, **kwargs),
        ):
            mount = runtime.start()
            try:
                self.assertTrue(mount.socket_path.is_socket())
            finally:
                runtime.close()

        self.assertEqual(order[:2], ["initialize", "bind"])
        self.assertEqual(
            load_pending(self.layout.family_pending_dir, pending.request_id).state,
            PendingState.UNKNOWN,
        )

    # Break caught: capability material being persisted to the host run directory.
    def test_context_creates_bounded_private_socket_and_cleans_owned_artifacts(self) -> None:
        runtime = self.runtime()

        with runtime as mount:
            run_dir = mount.socket_dir
            self.assertEqual(run_dir.parent, self.layout.family_intake_run_root)
            self.assertEqual(run_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(mount.socket_path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(stat.S_ISSOCK(mount.socket_path.lstat().st_mode))
            self.assertEqual(sorted(path.name for path in run_dir.iterdir()), ["intake.sock"])
            self.assertEqual(set(mount.environment), {
                "AGENT_FAMILY_SOCKET",
                "AGENT_FAMILY_CAPABILITY",
            })
            self.assertFalse(runtime.session.consumed)

        self.assertFalse(run_dir.exists())
        runtime.close()

    # Break caught: missing or malformed binding still enabling credential-free intake.
    def test_start_requires_valid_binding_and_leaves_no_live_socket(self) -> None:
        self.layout.family_binding_file.write_text("{}\n", encoding="ascii")
        self.layout.family_binding_file.chmod(0o600)
        runtime = self.runtime()

        with self.assertRaises(FamilyIntakeRuntimeError) as raised:
            runtime.start()

        self.assertEqual(str(raised.exception), "family intake runtime failed to start")
        self.assertFalse(runtime.is_alive())
        self.assertFalse(self.layout.family_intake_run_root.exists())

    # Break caught: a failure opening the just-created run directory leaking it forever.
    def test_run_directory_open_failure_cleans_the_inode_created_by_this_start(self) -> None:
        runtime = self.runtime()
        real_open = os.open
        run_id = "33" * 8

        def fail_run_open(path: object, flags: int, *args: object, **kwargs: object):
            if path == run_id and kwargs.get("dir_fd") is not None:
                raise OSError("private-open-marker")
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        with patch(
            "agent_container.family_intake_runtime.os.open",
            side_effect=fail_run_open,
        ):
            with self.assertRaises(FamilyIntakeRuntimeError):
                runtime.start()

        self.assertEqual(list(self.layout.family_intake_run_root.iterdir()), [])

    # Break caught: bind succeeding before chmod fails and leaving a live stale socket.
    def test_post_bind_start_failure_cleans_the_owned_socket_and_run_directory(self) -> None:
        runtime = self.runtime()
        real_chmod = os.chmod

        def fail_socket_chmod(path: object, mode: int, *args: object, **kwargs: object):
            if str(path) == "intake.sock":
                raise OSError("private-chmod-marker")
            return real_chmod(path, mode, *args, **kwargs)  # type: ignore[arg-type]

        with patch(
            "agent_container.family_intake_runtime.os.chmod",
            side_effect=fail_socket_chmod,
        ):
            with self.assertRaises(FamilyIntakeRuntimeError):
                runtime.start()

        self.assertEqual(list(self.layout.family_intake_run_root.iterdir()), [])

    # Break caught: cleanup deleting an attacker replacement at the socket name.
    def test_cleanup_preserves_replacement_inode_and_reports_fixed_failure(self) -> None:
        runtime = self.runtime()
        mount = runtime.start()
        mount.socket_path.unlink()
        mount.socket_path.write_text("replacement-marker", encoding="ascii")
        mount.socket_path.chmod(0o600)

        with self.assertRaises(FamilyIntakeRuntimeError) as raised:
            runtime.close()

        self.assertEqual(str(raised.exception), "family intake runtime cleanup failed")
        self.assertEqual(mount.socket_path.read_text("ascii"), "replacement-marker")
        self.assertNotIn("replacement-marker", str(raised.exception))

    # Break caught: environment or import graph introducing credential/network modules.
    def test_runtime_environment_and_intake_imports_contain_no_sensitive_surface(self) -> None:
        runtime = self.runtime()
        with runtime as mount:
            rendered = json.dumps(dict(mount.environment), sort_keys=True)
        for forbidden in (
            "repository",
            "private-key",
            "installation",
            "token",
            str(self.layout.family_pending_dir),
        ):
            self.assertNotIn(forbidden, rendered.lower())

        modules: set[str] = set()
        for name in (
                "family_intake_broker.py",
                "family_intake_transport.py",
                "family_intake_runtime.py",
        ):
            tree = ast.parse(
                (
                    Path(__file__).parents[2]
                    / "src"
                    / "agent_container"
                    / name
                ).read_text(encoding="utf-8")
            )
            modules.update(
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module is not None
            )
        for forbidden_import in (
            "agent_container.github_app",
            "agent_container.github_client",
            "agent_container.github_issue",
            "agent_container.github_broker_policy",
            "agent_container.github_broker_runtime",
        ):
            self.assertNotIn(forbidden_import, modules)


if __name__ == "__main__":
    unittest.main()
