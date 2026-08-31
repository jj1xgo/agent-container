from io import StringIO
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from agent_container.family_intake_client import connect_family_intake
from agent_container.family_intake_client import run
from agent_container.family_intake_protocol import encode_request_frame
from agent_container.family_intake_protocol import FamilyIntakeRequest
from agent_container.family_intake_runtime import FamilyIntakeRuntime
from agent_container.family_pending import list_pending
from agent_container.family_pending import inspect_pending_store
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

    def runtime(self) -> FamilyIntakeRuntime:
        return FamilyIntakeRuntime.create(self.layout)

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
            runtime.register_runtime(os.getpid())
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
        pending = list_pending(self.layout.family_pending_dir, "demo")
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
            second.register_runtime(os.getpid())
            self.assertNotEqual(stale, mount.capability)
            with self.assertRaises((OSError, RuntimeError, ValueError)):
                connect_family_intake(self.request(stale), mount.socket_path)
            response = connect_family_intake(
                self.request(mount.capability),
                mount.socket_path,
            )

        self.assertEqual(response.status, "pending")
        self.assertEqual(len(list_pending(self.layout.family_pending_dir, "demo")), 1)

    # Break caught: any local process with the capability being treated as the runtime.
    def test_registered_runtime_root_execs_client_while_sibling_is_denied(self) -> None:
        runtime = self.runtime()
        with runtime as mount:
            source_root = Path(__file__).parents[2]
            shim_dir = self.layout.root / "wrapper-test-bin"
            shim_dir.mkdir(mode=0o700)
            python_shim = shim_dir / "python3"
            python_shim.write_text(
                "#!/bin/sh\n"
                'PYTHONPATH="$AGENT_TEST_PYTHONPATH" '
                f'exec "{sys.executable}" "$@"\n',
                encoding="ascii",
            )
            python_shim.chmod(0o700)
            wrapper = source_root / "container" / "bin" / "agent-family"
            client_command = (
                "/bin/sh",
                str(wrapper),
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
            )
            root_script = (
                "import json,os,subprocess,sys; "
                "config=json.loads(sys.stdin.readline()); "
                "environment=dict(os.environ); environment.update(config['environment']); "
                "completed=subprocess.run(config['command'],"
                "env=environment,text=True,capture_output=True,check=False,timeout=5); "
                "print(json.dumps({'returncode':completed.returncode,"
                "'stdout':completed.stdout,'stderr':completed.stderr}))"
            )
            root = subprocess.Popen(
                (sys.executable, "-c", root_script),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            def stop_root() -> None:
                if root.poll() is None:
                    root.kill()
                    root.communicate(timeout=5)

            self.addCleanup(stop_root)
            runtime.register_runtime(root.pid)

            environment = dict(mount.environment)
            environment["AGENT_FAMILY_SOCKET"] = str(mount.socket_path)
            environment["AGENT_TEST_PYTHONPATH"] = str(source_root / "src")
            environment["PATH"] = str(shim_dir) + os.pathsep + os.environ["PATH"]
            denied = subprocess.run(
                client_command,
                env=dict(os.environ) | environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )

            self.assertEqual(denied.returncode, 1)
            self.assertEqual(denied.stdout, "")
            self.assertEqual(denied.stderr, "error: family intake request failed\n")
            self.assertFalse(runtime.session.consumed)  # type: ignore[union-attr]
            root_stdout, root_stderr = root.communicate(
                json.dumps(
                    {"command": client_command, "environment": environment},
                    sort_keys=True,
                )
                + "\n",
                timeout=5,
            )

        self.assertEqual(root.returncode, 0)
        self.assertEqual(root_stderr, "")
        result = json.loads(root_stdout)
        self.assertEqual(result["returncode"], 0)
        self.assertRegex(result["stdout"], r"^pending [0-9a-f]{32} [0-9]+\n$")
        self.assertEqual(result["stderr"], "")
        self.assertEqual(len(list_pending(self.layout.family_pending_dir, "demo")), 1)

    # Break caught: client disconnect boundaries either burning early or replaying late.
    def test_disconnect_before_and_after_persistence_have_defined_state(self) -> None:
        before = self.runtime()
        handler_entered = threading.Event()
        handler_completed = threading.Event()
        runtime_module = __import__(
            "agent_container.family_intake_runtime",
            fromlist=["handle_family_intake_connection"],
        )
        real_handler = runtime_module.handle_family_intake_connection

        def observed_handler(*args: object, **kwargs: object) -> None:
            handler_entered.set()
            try:
                real_handler(*args, **kwargs)
            finally:
                handler_completed.set()

        with patch(
            "agent_container.family_intake_runtime.handle_family_intake_connection",
            side_effect=observed_handler,
        ):
            with before as mount:
                before.register_runtime(os.getpid())
                frame = encode_request_frame(self.request(mount.capability))
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.connect(str(mount.socket_path))
                client.sendall(frame[:-1])
                self.assertTrue(handler_entered.wait(1), "handler did not accept request")
                client.close()
                self.assertTrue(
                    handler_completed.wait(1),
                    "incomplete request handler did not complete",
                )
                self.assertFalse(before.session.consumed)  # type: ignore[union-attr]
                response = connect_family_intake(
                    self.request(mount.capability), mount.socket_path
                )
        self.assertEqual(response.status, "pending")

        for path in self.layout.family_pending_dir.iterdir():
            path.unlink()
        after = self.runtime()
        with after as mount:
            after.register_runtime(os.getpid())
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(mount.socket_path))
            client.sendall(encode_request_frame(self.request(mount.capability)))
            client.close()
            self.wait_for(
                lambda: after.session.consumed,  # type: ignore[union-attr]
                "complete disconnected request was not persisted",
            )
        self.assertEqual(len(list_pending(self.layout.family_pending_dir, "demo")), 1)

    # Break caught: concurrent uses of one capability both creating durable records.
    def test_concurrent_clients_have_exactly_one_successful_pending_request(self) -> None:
        runtime = self.runtime()
        barrier = threading.Barrier(3)
        successes = []
        failures = []
        with runtime as mount:
            runtime.register_runtime(os.getpid())
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
        self.assertEqual(len(list_pending(self.layout.family_pending_dir, "demo")), 1)

    # Break caught: broker death silently routing through another network path.
    def test_broker_death_has_no_fallback_and_socket_is_gone(self) -> None:
        runtime = self.runtime()
        mount = runtime.start()
        runtime.register_runtime(os.getpid())
        runtime.close()

        with self.assertRaises((OSError, RuntimeError, ValueError)):
            connect_family_intake(
                self.request(mount.capability), mount.socket_path
            )
        self.assertFalse(mount.socket_dir.exists())
        self.assertEqual(list_pending(self.layout.family_pending_dir, "demo"), ())

    # Break caught: runtime exit waiting for the full client timeout on a partial frame.
    def test_runtime_exit_interrupts_a_client_stalled_mid_frame(self) -> None:
        runtime = self.runtime()
        mount = runtime.start()
        runtime.register_runtime(os.getpid())
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
        self.assertEqual(list_pending(self.layout.family_pending_dir, "demo"), ())

    # Break caught: a fatal handler exit leaving a live listener and queued clients.
    def test_unexpected_handler_failure_closes_service_and_reports_fixed_error(self) -> None:
        marker = "private-fatal-handler-marker"
        runtime = self.runtime()
        mount = runtime.start()
        runtime.register_runtime(os.getpid())
        first = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        first.settimeout(1)

        with patch(
            "agent_container.family_intake_runtime.handle_family_intake_connection",
            side_effect=RuntimeError(marker),
        ):
            first.connect(str(mount.socket_path))
            first.sendall(b"\x00\x00\x00\x01x")
            try:
                self.assertEqual(first.recv(1), b"")
            except OSError:
                pass
        first.close()
        self.wait_for(lambda: not runtime.is_alive(), "fatal handler remained alive")

        with self.assertRaisesRegex(Exception, "^family intake runtime failed$") as checked:
            runtime.check()
        self.assertNotIn(marker, str(checked.exception))
        second = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        second.settimeout(0.5)
        try:
            with self.assertRaises(OSError):
                second.connect(str(mount.socket_path))
        finally:
            second.close()
        with self.assertRaisesRegex(Exception, "^family intake runtime failed$") as closed:
            runtime.close()
        self.assertNotIn(marker, str(closed.exception))
        self.assertNotIn(marker, repr(runtime))
        self.assertFalse(mount.socket_dir.exists())

    # Break caught: create persistence failure looking like an ordinary client disconnect.
    def test_create_pending_failure_is_sanitized_fatal_runtime_failure(self) -> None:
        marker = "private-create-error-marker"
        runtime = self.runtime()
        mount = runtime.start()
        runtime.register_runtime(os.getpid())
        environment = dict(mount.environment)
        environment["AGENT_FAMILY_SOCKET"] = str(mount.socket_path)
        stdout = StringIO()
        stderr = StringIO()

        with patch(
            "agent_container.family_intake_broker.create_pending",
            side_effect=OSError(marker),
        ):
            status = run(
                (
                    "issue", "create", "--title", "Add export",
                    "--summary", "Portable copy.", "--context", "No export exists.",
                    "--acceptance-criterion", "JSON downloads",
                ),
                environment,
                stdout,
                stderr,
            )
        self.wait_for(lambda: not runtime.is_alive(), "persistence failure was not fatal")

        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "error: family intake request failed\n")
        with self.assertRaisesRegex(Exception, "^family intake runtime failed$") as checked:
            runtime.check()
        exposed = stderr.getvalue() + str(checked.exception) + repr(runtime)
        self.assertNotIn(marker, exposed)
        self.assertNotIn(mount.capability, exposed)
        with self.assertRaisesRegex(Exception, "^family intake runtime failed$"):
            runtime.close()
        self.assertFalse(mount.socket_dir.exists())

    # Break caught: audit durability failure leaving a normal-looking consumed service.
    def test_audit_failure_is_sanitized_fatal_runtime_failure(self) -> None:
        marker = "private-audit-error-marker"
        runtime = self.runtime()
        mount = runtime.start()
        runtime.register_runtime(os.getpid())
        request = self.request(mount.capability)

        with patch(
            "agent_container.family_pending.append_family_audit",
            side_effect=OSError(marker),
        ):
            with self.assertRaises((OSError, RuntimeError, ValueError)) as client_error:
                connect_family_intake(request, mount.socket_path)
        self.wait_for(lambda: not runtime.is_alive(), "audit failure was not fatal")

        with self.assertRaisesRegex(Exception, "^family intake runtime failed$") as checked:
            runtime.check()
        audit = (
            self.layout.family_audit_file.read_text("ascii")
            if self.layout.family_audit_file.exists()
            else ""
        )
        exposed = str(client_error.exception) + str(checked.exception) + repr(runtime) + audit
        self.assertNotIn(marker, exposed)
        self.assertNotIn(mount.capability, exposed)
        self.assertEqual(
            len(inspect_pending_store(self.layout.family_pending_dir, "demo")), 1
        )
        with self.assertRaisesRegex(ValueError, "audit"):
            list_pending(self.layout.family_pending_dir, "demo")
        with self.assertRaisesRegex(Exception, "^family intake runtime failed$"):
            runtime.close()
        self.assertFalse(mount.socket_dir.exists())


if __name__ == "__main__":
    unittest.main()
