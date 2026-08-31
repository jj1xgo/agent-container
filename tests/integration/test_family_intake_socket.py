from io import StringIO
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest

from agent_container.family_intake_client import connect_family_intake
from agent_container.family_intake_client import run
from agent_container.family_intake_protocol import encode_request_frame
from agent_container.family_intake_protocol import FamilyIntakeRequest
from agent_container.family_intake_runtime import FamilyIntakeRuntime
from agent_container.family_pending import list_pending
from agent_container.family_state import FamilyBinding
from agent_container.family_state import FamilyStateLayout
from agent_container.family_state import write_family_binding
from agent_container.state import Repository


SUPPORTED = (
    hasattr(socket, "AF_UNIX")
    and hasattr(socket, "SO_PEERCRED")
    and sys.platform.startswith("linux")
)


@unittest.skipUnless(SUPPORTED, "Linux SO_PEERCRED Unix sockets are required")
class FamilyIntakeSocketIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        root.chmod(0o700)
        self.layout = FamilyStateLayout(root, "demo")
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

    def runtime(self, *, peer_pid: int | None = None) -> FamilyIntakeRuntime:
        return FamilyIntakeRuntime.create(
            self.layout,
            os.getpid() if peer_pid is None else peer_pid,
        )

    def request(self, capability: str) -> FamilyIntakeRequest:
        return FamilyIntakeRequest(
            1,
            "issue_create_request",
            capability,
            {
                "title": "Add export",
                "summary": "Portable copy.",
                "context": "No export exists.",
                "acceptance_criteria": ["JSON downloads"],
            },
        )

    def wait_for(self, predicate, message: str) -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(message)

    # Break caught: the actual CLI flow differing from unit framing across a Unix socket.
    def test_cli_success_crosses_real_socket_and_runtime_cleans_up(self) -> None:
        runtime = self.runtime()
        stdout = StringIO()
        stderr = StringIO()

        with runtime as mount:
            run_dir = mount.socket_dir
            host_environment = dict(mount.environment)
            host_environment["AGENT_FAMILY_SOCKET"] = str(mount.socket_path)
            status = run(
                (
                    "issue",
                    "create",
                    "--title",
                    "Add export",
                    "--summary",
                    "Portable copy.",
                    "--context",
                    "No export exists.",
                    "--acceptance-criterion",
                    "JSON downloads",
                ),
                host_environment,
                stdout,
                stderr,
            )

        self.assertEqual(status, 0)
        pending = list_pending(self.layout.family_pending_dir)
        self.assertEqual(stdout.getvalue(), f"pending {pending[0].request_id} {pending[0].expires_at}\n")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(len(pending), 1)
        self.assertFalse(run_dir.exists())

    # Break caught: a capability from a dead run being accepted by its successor.
    def test_stale_capability_is_denied_then_current_capability_succeeds(self) -> None:
        first = self.runtime()
        first_mount = first.start()
        stale = first_mount.capability
        first.close()

        second = self.runtime()
        with second as mount:
            self.assertNotEqual(stale, mount.capability)
            with self.assertRaises((OSError, RuntimeError, ValueError)):
                connect_family_intake(self.request(stale), mount.socket_path)
            response = connect_family_intake(
                self.request(mount.capability),
                mount.socket_path,
            )

        self.assertEqual(response.status, "pending")
        self.assertEqual(len(list_pending(self.layout.family_pending_dir)), 1)

    # Break caught: any local process with the capability being treated as the runtime.
    def test_second_process_peer_pid_is_denied_without_burning_capability(self) -> None:
        runtime = self.runtime()
        with runtime as mount:
            script = (
                "import os,sys; from pathlib import Path; "
                "from agent_container.family_intake_client import run; "
                "raise SystemExit(run((\"issue\",\"create\",\"--title\",\"Add export\","
                "\"--summary\",\"Portable copy.\",\"--context\",\"No export exists.\","
                "\"--acceptance-criterion\",\"JSON downloads\"),os.environ,sys.stdout,sys.stderr))"
            )
            environment = dict(os.environ)
            environment.update(mount.environment)
            environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
            denied = subprocess.run(
                (sys.executable, "-c", script),
                env=environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )

            self.assertEqual(denied.returncode, 1)
            self.assertEqual(denied.stdout, "")
            self.assertEqual(denied.stderr, "error: family intake request failed\n")
            self.assertFalse(runtime.session.consumed)  # type: ignore[union-attr]
            response = connect_family_intake(
                self.request(mount.capability), mount.socket_path
            )

        self.assertEqual(response.status, "pending")

    # Break caught: client disconnect boundaries either burning early or replaying late.
    def test_disconnect_before_and_after_persistence_have_defined_state(self) -> None:
        before = self.runtime()
        with before as mount:
            frame = encode_request_frame(self.request(mount.capability))
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(mount.socket_path))
            client.sendall(frame[:-1])
            client.close()
            self.wait_for(
                lambda: not before.session.consumed,  # type: ignore[union-attr]
                "incomplete request unexpectedly consumed the session",
            )
            response = connect_family_intake(
                self.request(mount.capability), mount.socket_path
            )
        self.assertEqual(response.status, "pending")

        for path in self.layout.family_pending_dir.iterdir():
            path.unlink()
        after = self.runtime()
        with after as mount:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(mount.socket_path))
            client.sendall(encode_request_frame(self.request(mount.capability)))
            client.close()
            self.wait_for(
                lambda: after.session.consumed,  # type: ignore[union-attr]
                "complete disconnected request was not persisted",
            )
        self.assertEqual(len(list_pending(self.layout.family_pending_dir)), 1)

    # Break caught: concurrent uses of one capability both creating durable records.
    def test_concurrent_clients_have_exactly_one_successful_pending_request(self) -> None:
        runtime = self.runtime()
        barrier = threading.Barrier(3)
        successes = []
        failures = []
        with runtime as mount:
            def submit() -> None:
                barrier.wait()
                try:
                    successes.append(
                        connect_family_intake(
                            self.request(mount.capability), mount.socket_path
                        )
                    )
                except (OSError, RuntimeError, ValueError) as error:
                    failures.append(type(error).__name__)

            threads = [threading.Thread(target=submit) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(len(list_pending(self.layout.family_pending_dir)), 1)

    # Break caught: broker death silently routing through another network path.
    def test_broker_death_has_no_fallback_and_socket_is_gone(self) -> None:
        runtime = self.runtime()
        mount = runtime.start()
        runtime.close()

        with self.assertRaises((OSError, RuntimeError, ValueError)):
            connect_family_intake(
                self.request(mount.capability), mount.socket_path
            )
        self.assertFalse(mount.socket_dir.exists())
        self.assertEqual(list_pending(self.layout.family_pending_dir), ())

    # Break caught: runtime exit waiting for the full client timeout on a partial frame.
    def test_runtime_exit_interrupts_a_client_stalled_mid_frame(self) -> None:
        runtime = self.runtime()
        mount = runtime.start()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(mount.socket_path))
        client.sendall(b"\x00\x00")
        self.wait_for(
            lambda: runtime._client is not None,
            "runtime did not accept the stalled client",
        )

        started = time.monotonic()
        runtime.close()
        elapsed = time.monotonic() - started
        client.close()

        self.assertLess(elapsed, 2)
        self.assertFalse(mount.socket_dir.exists())
        self.assertEqual(list_pending(self.layout.family_pending_dir), ())


if __name__ == "__main__":
    unittest.main()
