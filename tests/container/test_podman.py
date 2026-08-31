from pathlib import Path
import os
import signal
import socket
import stat
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from agent_container.podman import auth_codex_spec
from agent_container.podman import BrokerRuntimeMount
from agent_container.podman import CommandSpec
from agent_container.podman import codex_login_status_spec
from agent_container.podman import build_image_spec
from agent_container.podman import build_project_image_spec
from agent_container.podman import claude_setup_token_spec
from agent_container.podman import claude_policy_status_spec
from agent_container.podman import claude_superpowers_spec
from agent_container.podman import claude_superpowers_marketplace_spec
from agent_container.podman import claude_token_status_spec
from agent_container.podman import cli_version_spec
from agent_container.podman import egress_adapter_status_spec
from agent_container.podman import clone_project_spec
from agent_container.podman import codex_superpowers_install_spec
from agent_container.podman import codex_superpowers_marketplace_spec
from agent_container.podman import podman_architecture_spec
from agent_container.podman import podman_image_id_spec
from agent_container.podman import podman_project_images_spec
from agent_container.podman import podman_running_agent_containers_spec
from agent_container.podman import podman_stats_spec
from agent_container.podman import run_codex_spec
from agent_container.podman import run_claude_spec
from agent_container.podman import run_command_supervised
from agent_container.podman import _wait_for_stopped_container
from agent_container.family_intake_runtime import FamilyRuntimeMount
from agent_container.family_intake_runtime import FamilyIntakeRuntime
from agent_container.egress_broker_runtime import EgressBrokerRuntimeError
from agent_container.handover_broker_runtime import HandoverRuntimeMount
from agent_container.egress_broker_runtime import EgressRuntimeMount
from agent_container.github_broker_policy import BrokerPolicy
from agent_container.state import Repository
from agent_container.state import StateLayout
from agent_container.state import github_broker_project_label


IMAGE = "localhost/agent-container:dev"
DERIVED = "localhost/agent-container-project:sotlas-frontend-0123456789abcdef"
HANDOVER_BROKER = HandoverRuntimeMount(Path("/state/handover-broker/one"))


class PodmanCommandTest(unittest.TestCase):
    def test_family_handoff_reports_podman_early_exit(self) -> None:
        process = mock.Mock()
        process.poll.side_effect = [None, 125]

        with mock.patch.object(
            Path, "read_text", side_effect=FileNotFoundError()
        ), mock.patch(
            "agent_container.podman.time.monotonic", side_effect=[0, 0, 0]
        ), mock.patch("agent_container.podman.time.sleep"), self.assertRaisesRegex(
            RuntimeError, "podman exited after pidfile missing"
        ):
            _wait_for_stopped_container(Path("/secret/container.pid"), process)

    def test_family_handoff_timeout_reports_last_safe_observation(self) -> None:
        class Process:
            def poll(self):
                return None

        cases = (
            (FileNotFoundError(), "pidfile missing"),
            ("not-a-pid", "pidfile invalid"),
        )
        for pidfile_result, expected in cases:
            with self.subTest(expected=expected), mock.patch.object(
                Path, "read_text", side_effect=[pidfile_result]
            ), mock.patch(
                "agent_container.podman.time.monotonic", side_effect=[0, 0, 11]
            ), mock.patch("agent_container.podman.time.sleep"):
                with self.assertRaisesRegex(RuntimeError, expected):
                    _wait_for_stopped_container(
                        Path("/secret/container.pid"), Process()
                    )

    def test_family_handoff_timeout_reports_unreadable_process_state(self) -> None:
        class Process:
            def poll(self):
                return None

        with mock.patch.object(
            Path,
            "read_text",
            side_effect=["123", FileNotFoundError()],
        ), mock.patch(
            "agent_container.podman.time.monotonic", side_effect=[0, 0, 11]
        ), mock.patch("agent_container.podman.time.sleep"), self.assertRaisesRegex(
            RuntimeError, "process state unavailable"
        ) as raised:
            _wait_for_stopped_container(Path("/secret/container.pid"), Process())

        self.assertNotIn("123", str(raised.exception))
        self.assertNotIn("/secret", str(raised.exception))

    def test_family_handoff_timeout_reports_process_not_stopped(self) -> None:
        class Process:
            def poll(self):
                return None

        with mock.patch.object(
            Path,
            "read_text",
            side_effect=["123", "123 (agent-runtime) S 1 2 3"],
        ), mock.patch(
            "agent_container.podman.time.monotonic", side_effect=[0, 0, 11]
        ), mock.patch("agent_container.podman.time.sleep"), self.assertRaisesRegex(
            RuntimeError, "process not stopped"
        ):
            _wait_for_stopped_container(Path("/secret/container.pid"), Process())

    def test_preserved_fd_reaches_fake_podman_and_oci_child(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            fake_podman = root / "fake-podman"
            fake_podman.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in --preserve-fd=*) fd=${1#*=} ;; *) exit 81 ;; esac\n"
                "exec python3 -c 'import os,stat,sys; "
                "assert stat.S_ISSOCK(os.fstat(int(sys.argv[1])).st_mode)' \"$fd\"\n",
                encoding="ascii",
            )
            fake_podman.chmod(0o755)
            socket_path = root / "intake.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            descriptor = os.open(
                socket_path,
                getattr(os, "O_PATH", os.O_RDONLY) | os.O_NOFOLLOW,
            )
            try:
                self.assertTrue(stat.S_ISSOCK(os.fstat(descriptor).st_mode))
                with self.assertRaises((NotADirectoryError, OSError)):
                    os.open("..", os.O_RDONLY, dir_fd=descriptor)
                completed = run_command_supervised(
                    CommandSpec(
                        (str(fake_podman), f"--preserve-fd={descriptor}"),
                        {},
                        (descriptor,),
                    ),
                    None,
                    None,
                    None,
                    None,
                )
            finally:
                os.close(descriptor)
                listener.close()

        self.assertEqual(completed.returncode, 0)

    def test_family_supervision_registers_stopped_runtime_before_resume(self) -> None:
        events = []

        class Process:
            pid = 4321
            returncode = 0

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        family = mock.create_autospec(FamilyIntakeRuntime, instance=True)
        family.check.return_value = None
        family.validate_mount.side_effect = lambda: events.append("revalidated")
        gateway = mock.Mock()
        gateway.wait_failed.return_value = False
        egress = EgressRuntimeMount(
            Path("/state/egress/codex"), "agent-container", "codex"
        )
        family_mount = mock.create_autospec(FamilyRuntimeMount, instance=True)
        family_mount.container_name = "agent-family-safe"
        with mock.patch(
            "agent_container.podman.subprocess.Popen", return_value=Process()
        ) as popen, mock.patch(
            "agent_container.podman._wait_for_stopped_container",
            side_effect=lambda pidfile, process: events.append(
                ("stopped", pidfile.name, process.pid)
            ) or 9876,
            create=True,
        ), mock.patch(
            "agent_container.podman.os.kill",
            side_effect=lambda pid, signum: events.append(("signal", pid, signum)),
        ):
            family.register_runtime.side_effect = lambda pid: events.append(
                ("registered", pid)
            )
            result = run_command_supervised(
                CommandSpec(("podman", "run", "image"), {}, (77,)),
                gateway,
                egress,
                family,
                family_mount,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            events,
            [
                "revalidated",
                ("stopped", "container.pid", 4321),
                ("registered", 9876),
                ("signal", 9876, signal.SIGCONT),
            ],
        )
        launched = popen.call_args.args[0]
        self.assertEqual(launched[:2], ("podman", "run"))
        self.assertTrue(launched[2].startswith("--pidfile="))
        self.assertEqual(launched[3:], ("image",))
        self.assertEqual(popen.call_args.kwargs["pass_fds"], (77,))

    def test_family_registration_failure_kills_stopped_runtime_without_resume(self) -> None:
        class Process:
            pid = 4321
            returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = -signal.SIGKILL
                return self.returncode

        family = mock.create_autospec(FamilyIntakeRuntime, instance=True)
        family_mount = mock.create_autospec(FamilyRuntimeMount, instance=True)
        family_mount.container_name = "agent-family-safe"
        family.register_runtime.side_effect = RuntimeError("registration failed")
        signals = []
        with mock.patch(
            "agent_container.podman.subprocess.Popen", return_value=Process()
        ), mock.patch(
            "agent_container.podman._wait_for_stopped_container",
            return_value=9876,
        ), mock.patch(
            "agent_container.podman.os.kill",
            side_effect=lambda pid, signum: signals.append((pid, signum)),
        ), mock.patch(
            "agent_container.podman.subprocess.run",
            return_value=subprocess.CompletedProcess((), 0),
        ), self.assertRaises(RuntimeError):
            run_command_supervised(
                CommandSpec(("podman", "run", "image"), {}),
                mock.Mock(),
                EgressRuntimeMount(
                    Path("/state/egress/codex"), "agent-container", "codex"
                ),
                family,
                family_mount,
            )

        self.assertEqual(signals, [])

    def test_family_health_failure_stops_then_kills_exact_named_container(self) -> None:
        class Process:
            pid = 4321
            returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                return None

            def kill(self):
                self.returncode = -signal.SIGKILL

            def wait(self, timeout=None):
                if self.returncode is None:
                    raise subprocess.TimeoutExpired(("podman", "run"), timeout)
                return self.returncode

        family = mock.create_autospec(FamilyIntakeRuntime, instance=True)
        family.check.side_effect = RuntimeError("broker failed")
        family_mount = mock.create_autospec(FamilyRuntimeMount, instance=True)
        family_mount.container_name = "agent-family-safe"
        cleanups = []
        with mock.patch(
            "agent_container.podman.subprocess.Popen", return_value=Process()
        ), mock.patch(
            "agent_container.podman._wait_for_stopped_container",
            return_value=9876,
        ), mock.patch("agent_container.podman.os.kill"), mock.patch(
            "agent_container.podman.time.sleep"
        ) as slept, mock.patch(
            "agent_container.podman.subprocess.run",
            side_effect=lambda argv, **_kwargs: cleanups.append(tuple(argv))
            or subprocess.CompletedProcess(argv, 1 if len(cleanups) == 1 else 0),
        ), self.assertRaisesRegex(RuntimeError, "broker failed"):
            run_command_supervised(
                CommandSpec(("podman", "run", "image"), {}),
                None,
                None,
                family,
                family_mount,
            )

        self.assertEqual(slept.call_args.args, (0.1,))
        self.assertEqual(
            cleanups,
            [
                ("podman", "stop", "--ignore", "--time=2", "agent-family-safe"),
                ("podman", "kill", "--signal=KILL", "agent-family-safe"),
            ],
        )

    def test_family_runtime_mount_is_exactly_bounded_for_both_agents(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        handover = Path("/handovers/agent-container")
        capability = "A" * 43
        run_dir = (
            Path("/state/family/intake/r")
            / github_broker_project_label(layout.project_id)
            / "0123456789abcdef"
        )
        family = FamilyRuntimeMount(
            run_dir,
            capability,
            {
                "AGENT_FAMILY_SOCKET": "/run/agent-family/intake.sock",
                "AGENT_FAMILY_CAPABILITY": capability,
            },
        )
        object.__setattr__(family, "_directory_descriptor", 77)
        object.__setattr__(family, "_socket_descriptor", 78)

        with mock.patch.object(FamilyRuntimeMount, "revalidate"):
            specs = (
                run_codex_spec(
                    layout, handover, IMAGE, os.getuid(), os.getgid(),
                    family_mount=family,
                ),
                run_claude_spec(
                    layout, handover, IMAGE, os.getuid(), os.getgid(),
                    HANDOVER_BROKER, family_mount=family,
                ),
            )
        for spec in specs:
            with self.subTest(agent=spec.argv[-1]):
                self.assertEqual(
                    spec.argv.count(
                        f"type=bind,src={run_dir}/intake.sock,"
                        "dst=/run/agent-family/intake.sock"
                    ),
                    1,
                )
                self.assertEqual(
                    spec.argv.count(
                        "AGENT_FAMILY_SOCKET=/run/agent-family/intake.sock"
                    ),
                    1,
                )
                self.assertEqual(
                    spec.argv.count(f"AGENT_FAMILY_CAPABILITY={capability}"), 1
                )
                self.assertNotIn("--preserve-fd=77", spec.argv)
                self.assertEqual(spec.pass_fds, ())
                rendered = " ".join(spec.argv)
                for forbidden in (
                    "family/app.json", "private-key", "installation-token",
                    "family/roadmap", "repository_id", "/pending",
                    "family issue approve",
                ):
                    self.assertNotIn(forbidden, rendered)

    def test_family_runtime_mount_rejects_unsafe_or_inconsistent_values(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        handover = Path("/handovers/agent-container")
        capability = "A" * 43
        family = object.__new__(FamilyRuntimeMount)
        object.__setattr__(family, "socket_dir", Path("relative"))
        object.__setattr__(family, "capability", capability)
        object.__setattr__(family, "environment", {
            "AGENT_FAMILY_SOCKET": "/run/agent-family/intake.sock",
            "AGENT_FAMILY_CAPABILITY": capability,
        })
        cases = (family,)
        for family in cases:
            with self.assertRaises(ValueError):
                run_codex_spec(
                    layout, handover, IMAGE, os.getuid(), os.getgid(),
                    family_mount=family,
                )

    def test_nonzero_family_runtime_stops_exact_container_before_reraising(self) -> None:
        class Process:
            pid = 4321
            returncode = 9

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        family = mock.create_autospec(FamilyIntakeRuntime, instance=True)
        mount = mock.create_autospec(FamilyRuntimeMount, instance=True)
        mount.container_name = "agent-family-safe"
        cleanups = []
        with mock.patch(
            "agent_container.podman.subprocess.Popen", return_value=Process()
        ), mock.patch(
            "agent_container.podman._wait_for_stopped_container",
            return_value=9876,
        ), mock.patch("agent_container.podman.os.kill"), mock.patch(
            "agent_container.podman.subprocess.run",
            side_effect=lambda argv, **_kwargs: cleanups.append(tuple(argv))
            or subprocess.CompletedProcess(argv, 0),
        ), self.assertRaises(subprocess.CalledProcessError) as raised:
            run_command_supervised(
                CommandSpec(("podman", "run", "image"), {}),
                None,
                None,
                family,
                mount,
            )

        self.assertEqual(raised.exception.returncode, 9)
        self.assertEqual(
            cleanups,
            [("podman", "stop", "--ignore", "--time=2", "agent-family-safe")],
        )

    def test_nonzero_family_with_egress_cleans_only_egress_container(self) -> None:
        class Process:
            pid = 4321
            returncode = 7

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        family = mock.create_autospec(FamilyIntakeRuntime, instance=True)
        mount = mock.create_autospec(FamilyRuntimeMount, instance=True)
        mount.container_name = "agent-family-wrong-target"
        egress = EgressRuntimeMount(
            Path("/state/egress/codex"), "agent-container", "codex"
        )
        cleanups = []
        with mock.patch(
            "agent_container.podman.subprocess.Popen", return_value=Process()
        ), mock.patch(
            "agent_container.podman._wait_for_stopped_container",
            return_value=9876,
        ), mock.patch("agent_container.podman.os.kill"), mock.patch(
            "agent_container.podman.subprocess.run",
            side_effect=lambda argv, **_kwargs: cleanups.append(tuple(argv))
            or subprocess.CompletedProcess(argv, 0),
        ), self.assertRaises(subprocess.CalledProcessError):
            run_command_supervised(
                CommandSpec(("podman", "run", "image"), {}),
                mock.Mock(wait_failed=lambda _timeout: False),
                egress,
                family,
                mount,
            )

        self.assertEqual(
            cleanups,
            [("podman", "stop", "--ignore", "--time=2", egress.container_name)],
        )

    def test_family_cleanup_stop_and_kill_failure_is_fatal(self) -> None:
        class Process:
            pid = 4321
            returncode = 5

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        family = mock.create_autospec(FamilyIntakeRuntime, instance=True)
        mount = mock.create_autospec(FamilyRuntimeMount, instance=True)
        mount.container_name = "agent-family-safe"
        cleanups = []
        with mock.patch(
            "agent_container.podman.subprocess.Popen", return_value=Process()
        ), mock.patch(
            "agent_container.podman._wait_for_stopped_container",
            return_value=9876,
        ), mock.patch("agent_container.podman.os.kill"), mock.patch(
            "agent_container.podman.subprocess.run",
            side_effect=lambda argv, **_kwargs: cleanups.append(tuple(argv))
            or subprocess.CompletedProcess(argv, 1),
        ), self.assertRaises(EgressBrokerRuntimeError):
            run_command_supervised(
                CommandSpec(("podman", "run", "image"), {}),
                None,
                None,
                family,
                mount,
            )

        self.assertEqual(len(cleanups), 2)
        self.assertTrue(all(call[-1] == "agent-family-safe" for call in cleanups))

    def test_supervised_process_stops_named_container_when_cli_cannot_be_reaped(
        self,
    ) -> None:
        class Process:
            returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                return None

            def kill(self):
                return None

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(("podman", "run"), timeout)

        egress = EgressRuntimeMount(
            Path("/state/egress/codex"), "agent-container", "codex"
        )
        cleanup_result = subprocess.CompletedProcess(("podman", "stop"), 0)
        gateway = mock.Mock()
        gateway.wait_failed.return_value = True
        spec = CommandSpec(("podman", "run", "image"), {})
        with mock.patch(
            "agent_container.podman.subprocess.Popen", return_value=Process()
        ), mock.patch(
            "agent_container.podman.subprocess.run", return_value=cleanup_result
        ) as cleanup, self.assertRaises(EgressBrokerRuntimeError):
            run_command_supervised(spec, gateway, egress)

        self.assertEqual(cleanup.call_count, 1)
        self.assertEqual(
            cleanup.call_args.args[0],
            ("podman", "stop", "--ignore", "--time=2", egress.container_name),
        )

    def test_supervised_process_kills_named_container_after_cli_timeout(self) -> None:
        class Process:
            def __init__(self) -> None:
                self.returncode = None
                self.wait_count = 0

            def poll(self):
                return self.returncode

            def terminate(self):
                return None

            def kill(self):
                self.returncode = -signal.SIGKILL

            def wait(self, timeout=None):
                self.wait_count += 1
                if self.wait_count == 1:
                    raise subprocess.TimeoutExpired(("podman", "run"), timeout)
                return self.returncode

        egress = EgressRuntimeMount(
            Path("/state/egress/codex"), "agent-container", "codex"
        )
        process = Process()
        gateway = mock.Mock()
        gateway.wait_failed.return_value = True
        stop_timeout = subprocess.TimeoutExpired(
            ("podman", "stop", egress.container_name), 5
        )
        kill_result = subprocess.CompletedProcess(
            ("podman", "kill", egress.container_name), 0
        )
        spec = CommandSpec(("podman", "run", "image"), {})
        with mock.patch(
            "agent_container.podman.subprocess.Popen", return_value=process
        ), mock.patch(
            "agent_container.podman.subprocess.run",
            side_effect=(stop_timeout, kill_result),
        ) as cleanup, self.assertRaises(EgressBrokerRuntimeError):
            run_command_supervised(spec, gateway, egress)

        self.assertEqual(process.returncode, -signal.SIGKILL)
        self.assertEqual(
            [call.args[0] for call in cleanup.call_args_list],
            [
                ("podman", "stop", "--ignore", "--time=2", egress.container_name),
                ("podman", "kill", "--signal=KILL", egress.container_name),
            ],
        )
        self.assertTrue(all(call.kwargs["timeout"] == 5 for call in cleanup.call_args_list))

    def test_supervised_process_reaps_child_before_returning_from_sigterm(self) -> None:
        class Process:
            def __init__(self) -> None:
                self.returncode = None
                self.terminated = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -signal.SIGTERM

            def kill(self):
                self.returncode = -signal.SIGKILL

            def wait(self, timeout=None):
                return self.returncode

        handlers = {}
        restored = []

        def install_handler(signum, handler):
            if callable(handler):
                handlers[signum] = handler
            else:
                restored.append((signum, handler))

        def interrupt(_timeout):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return False

        process = Process()
        gateway = mock.Mock()
        gateway.wait_failed.side_effect = interrupt
        spec = CommandSpec(("podman", "run", "image"), {})
        with mock.patch(
            "agent_container.podman.subprocess.Popen", return_value=process
        ), mock.patch(
            "agent_container.podman.subprocess.run",
            return_value=subprocess.CompletedProcess(("podman", "stop"), 0),
        ), mock.patch(
            "agent_container.podman.signal.getsignal", return_value=signal.SIG_DFL
        ), mock.patch(
            "agent_container.podman.signal.signal", side_effect=install_handler
        ), self.assertRaises(EgressBrokerRuntimeError):
            run_command_supervised(
                spec,
                gateway,
                EgressRuntimeMount(
                    Path("/state/egress/codex"), "agent-container", "codex"
                ),
            )

        self.assertTrue(process.terminated)
        self.assertIsNotNone(process.returncode)
        self.assertEqual(
            restored,
            [
                (signal.SIGINT, signal.SIG_DFL),
                (signal.SIGTERM, signal.SIG_DFL),
                (signal.SIGHUP, signal.SIG_DFL),
            ],
        )

    def test_supervised_process_is_reaped_on_gateway_failure_and_interrupt(self) -> None:
        class Process:
            def __init__(self) -> None:
                self.returncode = None
                self.terminated = False
                self.killed = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def kill(self):
                self.killed = True
                self.returncode = -9

            def wait(self, timeout=None):
                return self.returncode

        spec = CommandSpec(("podman", "run", "image"), {})
        for failure in (True, KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                process = Process()
                gateway = mock.Mock()
                if isinstance(failure, BaseException):
                    gateway.wait_failed.side_effect = failure
                    expected = KeyboardInterrupt
                else:
                    gateway.wait_failed.return_value = True
                    expected = EgressBrokerRuntimeError
                with mock.patch(
                    "agent_container.podman.subprocess.Popen", return_value=process
                ), mock.patch(
                    "agent_container.podman.subprocess.run",
                    return_value=subprocess.CompletedProcess(("podman", "stop"), 0),
                ), self.assertRaises(expected):
                    run_command_supervised(
                        spec,
                        gateway,
                        EgressRuntimeMount(
                            Path("/state/egress/codex"), "agent-container", "codex"
                        ),
                    )
                self.assertTrue(process.terminated)
                self.assertIsNotNone(process.returncode)

    def test_enabled_runtime_wraps_both_agents_with_fixed_egress_contract(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        handover = Path("/vault/handovers/agent-container")
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent):
                egress = EgressRuntimeMount(
                    Path(f"/state/egress/{agent}"), "agent-container", agent
                )
                if agent == "codex":
                    spec = run_codex_spec(
                        layout,
                        handover,
                        IMAGE,
                        os.getuid(),
                        os.getgid(),
                        egress=egress,
                    )
                else:
                    spec = run_claude_spec(
                        layout,
                        handover,
                        IMAGE,
                        os.getuid(),
                        os.getgid(),
                        HANDOVER_BROKER,
                        egress=egress,
                    )
                self.assertEqual(spec.argv.count("--network=none"), 1)
                self.assertEqual(
                    sum(argument.startswith("--name=agent-egress-") for argument in spec.argv),
                    1,
                )
                joined = " ".join(spec.argv)
                self.assertIn(
                    f"src=/state/egress/{agent},dst=/run/agent-egress,ro=true",
                    joined,
                )
                for variable, value in (
                    ("AGENT_EGRESS_SOCKET", "/run/agent-egress/broker.sock"),
                    ("AGENT_EGRESS_CAPABILITY", "/run/agent-egress/capability"),
                    ("AGENT_EGRESS_AGENT", agent),
                    ("HTTPS_PROXY", "http://127.0.0.1:17843"),
                    ("https_proxy", "http://127.0.0.1:17843"),
                    ("HTTP_PROXY", "http://127.0.0.1:17843"),
                    ("http_proxy", "http://127.0.0.1:17843"),
                    ("NO_PROXY", "localhost,127.0.0.1,::1"),
                    ("no_proxy", "localhost,127.0.0.1,::1"),
                ):
                    self.assertEqual(spec.argv.count(f"{variable}={value}"), 1)
                image_index = spec.argv.index(IMAGE)
                self.assertEqual(
                    spec.argv[image_index + 1 : image_index + 5],
                    (
                        "agent-runtime-launcher", "--",
                        "agent-egress-runtime", "--",
                    ),
                )
                original = "codex" if agent == "codex" else "python3"
                self.assertEqual(spec.argv[image_index + 5], original)

    def test_egress_mount_rejects_relative_or_wrong_project_agent(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        handover = Path("/vault/handovers/agent-container")
        for egress in (
            EgressRuntimeMount(Path("relative"), "agent-container", "codex"),
            EgressRuntimeMount(Path("/state/egress"), "other", "codex"),
            EgressRuntimeMount(Path("/state/egress"), "agent-container", "claude"),
        ):
            with self.subTest(egress=egress), self.assertRaises(ValueError):
                run_codex_spec(
                    layout,
                    handover,
                    IMAGE,
                    os.getuid(),
                    os.getgid(),
                    egress=egress,
                )

    def test_egress_adapter_probe_is_hardened_noninteractive_and_mount_free(self) -> None:
        spec = egress_adapter_status_spec("example/image:current")
        self.assertEqual(
            spec.argv,
            (
                "podman",
                "run",
                "--rm",
                "--read-only",
                "--cap-drop=all",
                "--security-opt=no-new-privileges",
                "--userns=keep-id:uid=1000,gid=1000",
                "--tmpfs=/tmp:rw,nosuid,nodev,size=512m",
                "--network=none",
                "example/image:current",
                "agent-egress-runtime",
                "--self-check",
            ),
        )
        self.assertNotIn("--mount", spec.argv)
        self.assertNotIn("--env", spec.argv)
        self.assertEqual(spec.environment, {})

    def test_resource_monitor_commands_are_project_and_agent_scoped(self) -> None:
        listed = podman_running_agent_containers_spec("agent-container", "codex")
        self.assertEqual(
            listed.argv,
            (
                "podman",
                "ps",
                "--filter",
                "label=io.agent-container.managed=true",
                "--filter",
                "label=io.agent-container.project=agent-container",
                "--filter",
                "label=io.agent-container.agent=codex",
                "--format",
                "{{.ID}}",
            ),
        )

        stats = podman_stats_spec("0123456789ab")
        self.assertEqual(stats.argv[:3], ("podman", "stats", "--no-stream"))
        rendered = " ".join(stats.argv)
        self.assertIn("{{.CPUPerc}}", rendered)
        self.assertIn("{{.MemUsage}}", rendered)
        self.assertIn("{{.PIDs}}", rendered)
        self.assertIn("{{.UpTime}}", rendered)
        self.assertEqual(stats.argv[-1], "0123456789ab")

        with self.assertRaises(ValueError):
            podman_running_agent_containers_spec("agent-container", "other")
        with self.assertRaises(ValueError):
            podman_stats_spec("not-an-id")

    def test_superpowers_commands_use_agent_specific_project_state(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")

        marketplace = " ".join(
            codex_superpowers_marketplace_spec(layout, IMAGE).argv
        )
        self.assertNotIn("--interactive", marketplace)
        self.assertNotIn("--tty", marketplace)
        self.assertIn(
            "src=/state/projects/agent-container/codex-home,dst=/home/agent/.codex",
            marketplace,
        )
        self.assertIn(
            "codex plugin marketplace add obra/superpowers --ref main --json",
            marketplace,
        )
        install = " ".join(codex_superpowers_install_spec(layout, IMAGE).argv)
        self.assertIn("superpowers@superpowers-dev", install)

        update = " ".join(
            codex_superpowers_marketplace_spec(layout, IMAGE, update=True).argv
        )
        self.assertIn("marketplace upgrade superpowers-dev --json", update)

        claude_marketplace = " ".join(
            claude_superpowers_marketplace_spec(layout, IMAGE).argv
        )
        self.assertIn(
            "claude plugin marketplace add anthropics/claude-plugins-official",
            claude_marketplace,
        )
        claude_marketplace_update = " ".join(
            claude_superpowers_marketplace_spec(layout, IMAGE, update=True).argv
        )
        self.assertIn(
            "claude plugin marketplace update claude-plugins-official",
            claude_marketplace_update,
        )

        claude = " ".join(claude_superpowers_spec(layout, IMAGE).argv)
        self.assertIn(
            "src=/state/projects/agent-container/claude-config,dst=/home/agent/.claude",
            claude,
        )
        self.assertIn(
            "claude plugin install superpowers@claude-plugins-official --scope user --yes",
            claude,
        )
        claude_update = " ".join(
            claude_superpowers_spec(layout, IMAGE, update=True).argv
        )
        self.assertIn("claude plugin update superpowers@claude-plugins-official", claude_update)

    def test_claude_policy_probe_is_hardened_and_mount_free(self) -> None:
        spec = claude_policy_status_spec(IMAGE)

        self.assertEqual(
            spec.argv[-3:],
            ("python3", "-m", "agent_container.claude_policy"),
        )
        self.assertNotIn("--mount", spec.argv)
        for required in (
            "--rm",
            "--read-only",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--userns=keep-id:uid=1000,gid=1000",
        ):
            self.assertIn(required, spec.argv)

    def test_project_image_inspection_and_build_commands(self) -> None:
        self.assertEqual(
            podman_image_id_spec(IMAGE).argv,
            ("podman", "image", "inspect", "--format", "{{.Id}}", IMAGE),
        )
        self.assertEqual(
            podman_architecture_spec().argv,
            ("podman", "info", "--format", "{{.Host.Arch}}"),
        )
        self.assertEqual(
            podman_project_images_spec("sotlas-frontend").argv,
            (
                "podman",
                "images",
                "--filter",
                "reference=localhost/agent-container-project:sotlas-frontend-*",
                "--format",
                "{{.Repository}}:{{.Tag}}",
            ),
        )

        spec = build_project_image_spec(
            Path("/ctx"), Path("/ctx/Containerfile"), IMAGE, DERIVED
        )
        self.assertEqual(
            spec.argv,
            (
                "podman",
                "build",
                "--pull=never",
                "--build-arg",
                f"BASE_IMAGE={IMAGE}",
                "--tag",
                DERIVED,
                "--file",
                "/ctx/Containerfile",
                "/ctx",
            ),
        )
        self.assertEqual(spec.environment, {})

    def test_build_uses_versions_cachebuster_and_repository_context(self) -> None:
        spec = build_image_spec(
            Path("/repo"),
            IMAGE,
            "22.23.1",
            "0.149.0",
            "1.2.3",
            "12345",
            "0.2.0-dev.7+gabcdef0",
        )
        self.assertEqual(
            spec.argv,
            (
                "podman",
                "build",
                "--build-arg",
                "NODE_VERSION=22.23.1",
                "--build-arg",
                "CODEX_VERSION=0.149.0",
                "--build-arg",
                "CLAUDE_VERSION=1.2.3",
                "--build-arg",
                "AGENT_CLI_CACHEBUST=12345",
                "--build-arg",
                "AGENT_CONTAINER_VERSION=0.2.0-dev.7+gabcdef0",
                "--tag",
                IMAGE,
                "--file",
                "/repo/Containerfile",
                "/repo",
            ),
        )

    def test_cli_version_probes_are_hardened_and_mount_free(self) -> None:
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent):
                spec = cli_version_spec(IMAGE, agent)

                self.assertEqual(spec.argv[-2:], (agent, "--version"))
                self.assertNotIn("--mount", spec.argv)
                for required in (
                    "--rm",
                    "--read-only",
                    "--cap-drop=all",
                    "--security-opt=no-new-privileges",
                    "--userns=keep-id:uid=1000,gid=1000",
                    "--tmpfs=/tmp:rw,nosuid,nodev,size=512m",
                ):
                    self.assertIn(required, spec.argv)

    def test_node_version_probe_is_hardened_and_mount_free(self) -> None:
        from agent_container.podman import node_version_spec

        spec = node_version_spec(IMAGE)

        self.assertEqual(
            spec.argv[-2:], ("/opt/agent-node/bin/node", "--version")
        )
        self.assertNotIn("--mount", spec.argv)
        for required in (
            "--rm",
            "--read-only",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--userns=keep-id:uid=1000,gid=1000",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=512m",
        ):
                self.assertIn(required, spec.argv)

    def test_project_node_version_probe_uses_fixed_project_path(self) -> None:
        from agent_container.podman import project_node_version_spec

        spec = project_node_version_spec(IMAGE)

        self.assertEqual(
            spec.argv[-2:], ("/opt/project-node/bin/node", "--version")
        )
        self.assertNotIn("--mount", spec.argv)

    def test_auth_mounts_only_shared_codex_auth_directory(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        spec = auth_codex_spec(layout, IMAGE)
        joined = " ".join(spec.argv)
        self.assertIn("src=/state/shared-auth/codex,dst=/home/agent/.codex", joined)
        self.assertIn("codex login --device-auth", joined)
        self.assertNotIn("/workspace", joined)

    def test_login_status_uses_the_same_sanitized_auth_container(self) -> None:
        layout = StateLayout(Path("/state"), "auth")

        spec = codex_login_status_spec(layout, IMAGE)

        joined = " ".join(spec.argv)
        self.assertIn("src=/state/shared-auth/codex,dst=/home/agent/.codex", joined)
        self.assertEqual(spec.argv[-3:], ("codex", "login", "status"))
        self.assertNotIn("/workspace", joined)
        self.assertNotIn("token", joined.lower())

    def test_claude_setup_token_uses_only_ephemeral_claude_config(self) -> None:
        setup = claude_setup_token_spec(IMAGE)

        self.assertEqual(setup.argv[-2:], ("claude", "setup-token"))
        self.assertIn(
            "--tmpfs=/home/agent/.claude:rw,nosuid,nodev,noexec,size=16m",
            setup.argv,
        )
        self.assertIn("CLAUDE_CONFIG_DIR=/home/agent/.claude", setup.argv)
        self.assertNotIn("--mount", setup.argv)

    def test_claude_token_status_mounts_only_the_staged_token_read_only(self) -> None:
        status = claude_token_status_spec(Path("/private/staged"), IMAGE)

        joined = " ".join(status.argv)
        self.assertIn(
            "src=/private/staged,dst=/run/secrets/claude-oauth-token,ro=true",
            joined,
        )
        self.assertIn(
            "--tmpfs=/home/agent/.claude:rw,nosuid,nodev,noexec,size=16m",
            status.argv,
        )
        self.assertIn("CLAUDE_CONFIG_DIR=/home/agent/.claude", joined)
        self.assertEqual(
            status.argv[-9:],
            (
                IMAGE,
                "python3",
                "-m",
                "agent_container.claude_launcher",
                "/run/secrets/claude-oauth-token",
                "--",
                "claude",
                "auth",
                "status",
            ),
        )
        self.assertNotIn("/workspace", joined)

    def test_clone_uses_read_only_gh_config_without_credential_content(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        repository = Repository.parse("jj1xgo/agent-container")
        spec = clone_project_spec(layout, repository, IMAGE)
        joined = " ".join(spec.argv)
        self.assertIn("src=/state/gh,dst=/home/agent/.config/gh,ro=true", joined)
        self.assertIn("jj1xgo/agent-container", joined)
        self.assertIn("/workspaces/agent-container", spec.argv)
        self.assertNotIn("credential-value", joined)
        self.assertNotIn("token", joined.lower())

    def test_clone_can_use_only_project_scoped_broker_runtime(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        repository = Repository.parse("jj1xgo/agent-container")
        broker = BrokerRuntimeMount(Path("/state/runtime/one"), repository)

        spec = clone_project_spec(layout, repository, IMAGE, broker)
        joined = " ".join(spec.argv)

        self.assertIn(
            "src=/state/runtime/one,dst=/run/agent-broker,ro=true", joined
        )
        self.assertIn("AGENT_BROKER_SOCKET=/run/agent-broker/broker.sock", joined)
        self.assertIn(
            "AGENT_BROKER_CAPABILITY=/run/agent-broker/capability", joined
        )
        self.assertIn("AGENT_BROKER_REPOSITORY=jj1xgo/agent-container", joined)
        self.assertIn(
            "GIT_CONFIG_KEY_0=url.agent-broker://jj1xgo/agent-container.insteadOf",
            joined,
        )
        self.assertIn(
            "GIT_CONFIG_VALUE_0=https://github.com/jj1xgo/agent-container.git",
            joined,
        )
        self.assertEqual(
            spec.argv[-4:],
            (
                "git",
                "clone",
                "https://github.com/jj1xgo/agent-container.git",
                "/workspaces/agent-container",
            ),
        )
        self.assertNotIn("/state/gh", joined)
        self.assertNotIn("gh auth git-credential", joined)

    def test_bound_repository_id_is_absent_from_container_argv_and_mounts(self) -> None:
        repository_id = 987_654_321
        policy = BrokerPolicy.create(
            project_id="agent-container",
            repository="jj1xgo/agent-container",
            repository_id=repository_id,
            default_branch="main",
            protected_branches=("main",),
            require_repository_id=True,
        )
        layout = StateLayout(Path("/state"), policy.project_id)
        broker = BrokerRuntimeMount(Path("/state/runtime/one"), policy.repository)
        self.assertEqual(policy.repository_id, repository_id)
        self.assertEqual(set(vars(broker)), {"run_dir", "repository"})
        self.assertNotIn(str(repository_id), repr(broker))
        specs = (
            clone_project_spec(layout, policy.repository, IMAGE, broker),
            run_codex_spec(
                layout,
                Path("/vault/handovers/agent-container"),
                IMAGE,
                os.getuid(),
                os.getgid(),
                broker,
            ),
            run_claude_spec(
                layout,
                Path("/vault/handovers/agent-container"),
                IMAGE,
                os.getuid(),
                os.getgid(),
                HANDOVER_BROKER,
                broker,
            ),
        )

        for spec in specs:
            with self.subTest(command=spec.argv[-1]):
                rendered = " ".join(spec.argv)
                self.assertNotIn(str(repository_id), rendered)
                self.assertNotIn("repository_id", rendered)
                self.assertNotIn("repository-id", rendered)

    def test_runtime_can_replace_gh_mount_with_broker_runtime(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        repository = Repository.parse("jj1xgo/agent-container")
        broker = BrokerRuntimeMount(Path("/state/runtime/one"), repository)

        spec = run_codex_spec(
            layout,
            Path("/vault/handovers/agent-container"),
            IMAGE,
            os.getuid(),
            os.getgid(),
            broker,
        )
        joined = " ".join(spec.argv)

        self.assertIn("src=/state/runtime/one,dst=/run/agent-broker,ro=true", joined)
        self.assertNotIn("src=/state/gh", joined)
        self.assertNotIn("GH_CONFIG_DIR", joined)
        self.assertNotIn("gh auth git-credential", joined)

    def test_agent_runtimes_have_exact_resource_monitor_labels(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        handover = Path("/handovers/agent-container")
        broker = BrokerRuntimeMount(
            Path("/state/runtime/one"),
            Repository.parse("jj1xgo/agent-container"),
        )

        for agent, spec in (
            ("codex", run_codex_spec(layout, handover, IMAGE, os.getuid(), os.getgid())),
            (
                "claude",
                run_claude_spec(
                    layout,
                    handover,
                    IMAGE,
                    os.getuid(),
                    os.getgid(),
                    HANDOVER_BROKER,
                ),
            ),
        ):
            with self.subTest(agent=agent):
                joined = " ".join(spec.argv)
                self.assertIn("io.agent-container.managed=true", joined)
                self.assertIn("io.agent-container.project=agent-container", joined)
                self.assertIn(f"io.agent-container.agent={agent}", joined)

        claude = run_claude_spec(
            layout,
            Path("/vault/handovers/agent-container"),
            IMAGE,
            os.getuid(),
            os.getgid(),
            HANDOVER_BROKER,
            broker,
        )
        claude_joined = " ".join(claude.argv)
        self.assertIn(
            "src=/state/runtime/one,dst=/run/agent-broker,ro=true",
            claude_joined,
        )
        self.assertNotIn("src=/state/gh", claude_joined)
        self.assertNotIn("GH_CONFIG_DIR", claude_joined)

    def test_broker_mount_rejects_relative_path_and_wrong_repository(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        repository = Repository.parse("jj1xgo/agent-container")
        with self.assertRaisesRegex(ValueError, "absolute"):
            run_claude_spec(
                layout,
                Path("/vault/handovers/agent-container"),
                IMAGE,
                os.getuid(),
                os.getgid(),
                HandoverRuntimeMount(Path("relative")),
            )
        with self.assertRaisesRegex(ValueError, "absolute"):
            clone_project_spec(
                layout,
                repository,
                IMAGE,
                BrokerRuntimeMount(Path("relative"), repository),
            )
        with self.assertRaisesRegex(ValueError, "does not match"):
            clone_project_spec(
                layout,
                repository,
                IMAGE,
                BrokerRuntimeMount(
                    Path("/state/runtime/one"), Repository.parse("jj1xgo/other")
                ),
            )

    def test_run_has_hardened_flags_and_narrow_mounts(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        spec = run_codex_spec(
            layout=layout,
            handover_project=Path("/vault/handovers/agent-container"),
            image=IMAGE,
            uid=os.getuid(),
            gid=os.getgid(),
        )
        joined = " ".join(spec.argv)
        for required in ("--rm", "--read-only", "--cap-drop=all", "no-new-privileges"):
            self.assertIn(required, spec.argv if required != "no-new-privileges" else joined)
        self.assertIn("src=/state/workspaces/agent-container,dst=/workspace", joined)
        self.assertIn("src=/vault/handovers/agent-container,dst=/handovers/agent-container", joined)
        self.assertNotIn("/vault,dst=", joined)
        self.assertNotIn("token", joined.lower())
        self.assertEqual(
            spec.argv[-7:],
            (
                IMAGE,
                "agent-runtime-launcher",
                "--",
                "codex",
                "--approve-for-me",
                "-c",
                'tui.status_line=["model-with-reasoning","context-remaining",'
                '"five-hour-limit","weekly-limit","git-branch","project-name"]',
            ),
        )

    def test_claude_relocates_gh_config_without_changing_codex(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        handover_project = Path("/vault/handovers/agent-container")

        claude = run_claude_spec(
            layout,
            handover_project,
            IMAGE,
            os.getuid(),
            os.getgid(),
            HANDOVER_BROKER,
        )
        codex = run_codex_spec(
            layout,
            handover_project,
            IMAGE,
            os.getuid(),
            os.getgid(),
        )

        claude_joined = " ".join(claude.argv)
        self.assertIn("GH_CONFIG_DIR=/home/agent/gh-config", claude.argv)
        self.assertIn(
            "type=bind,src=/state/gh,dst=/home/agent/gh-config,ro=true",
            claude.argv,
        )
        self.assertNotIn("/home/agent/.config/gh", claude_joined)
        self.assertIn("GH_CONFIG_DIR=/home/agent/.config/gh", codex.argv)
        self.assertIn(
            "type=bind,src=/state/gh,dst=/home/agent/.config/gh,ro=true",
            codex.argv,
        )

    def test_run_rejects_uid_or_gid_other_than_current_process(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        handover_project = Path("/vault/handovers/agent-container")
        with self.assertRaisesRegex(ValueError, "current user"):
            run_codex_spec(
                layout,
                handover_project,
                IMAGE,
                os.getuid() + 1,
                os.getgid(),
            )
        with self.assertRaisesRegex(ValueError, "current user"):
            run_codex_spec(
                layout,
                handover_project,
                IMAGE,
                os.getuid(),
                os.getgid() + 1,
            )

    def test_claude_run_has_hardened_flags_and_isolated_mounts(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")

        spec = run_claude_spec(
            layout=layout,
            handover_project=Path("/vault/handovers/agent-container"),
            image=IMAGE,
            uid=os.getuid(),
            gid=os.getgid(),
            handover_broker=HANDOVER_BROKER,
        )

        joined = " ".join(spec.argv)
        for required in ("--rm", "--read-only", "--cap-drop=all", "no-new-privileges"):
            self.assertIn(required, spec.argv if required != "no-new-privileges" else joined)
        for source, target in (
            ("/state/workspaces/agent-container", "/workspace"),
            ("/state/projects/agent-container/claude-config", "/home/agent/.claude"),
            (
                "/state/shared-auth/claude/oauth-token",
                "/run/secrets/claude-oauth-token",
            ),
            ("/state/projects/agent-container/cache", "/home/agent/.cache"),
            ("/state/gh", "/home/agent/gh-config"),
            ("/vault/handovers/agent-container", "/handovers/agent-container"),
        ):
            self.assertIn(f"src={source},dst={target}", joined)
        self.assertIn(
            "type=bind,src=/state/projects/agent-container/claude-config,dst=/home/agent/.claude",
            spec.argv,
        )
        self.assertIn("src=/state/gh,dst=/home/agent/gh-config,ro=true", joined)
        self.assertIn(
            "src=/state/shared-auth/claude/oauth-token,dst=/run/secrets/claude-oauth-token,ro=true",
            joined,
        )
        self.assertIn("CLAUDE_CONFIG_DIR=/home/agent/.claude", joined)
        self.assertIn("AGENT_PROJECT_ID=agent-container", joined)
        self.assertIn("AGENT_HANDOVER_ROOT=/handovers", joined)
        self.assertIn(
            "src=/vault/handovers/agent-container,dst=/handovers/agent-container,ro=true",
            joined,
        )
        self.assertIn(
            "src=/state/handover-broker/one,dst=/run/agent-handover,ro=true",
            joined,
        )
        self.assertIn(
            "AGENT_HANDOVER_BROKER_SOCKET=/run/agent-handover/broker.sock",
            joined,
        )
        self.assertIn(
            "AGENT_HANDOVER_BROKER_CAPABILITY=/run/agent-handover/capability",
            joined,
        )
        self.assertNotIn(
            "src=/vault/handovers/agent-container,dst=/handovers/agent-container,rw",
            joined,
        )
        self.assertNotIn("src=/vault/handovers,dst=", joined)
        self.assertNotIn("/vault/handovers/other-project", joined)
        self.assertNotIn("capability-secret-value", joined)
        self.assertEqual(
            spec.argv[-9:],
            (
                IMAGE,
                "agent-runtime-launcher",
                "--",
                "python3",
                "-m",
                "agent_container.claude_launcher",
                "/run/secrets/claude-oauth-token",
                "--",
                "claude",
            ),
        )
        self.assertNotIn(".credentials.json", joined)
        self.assertNotIn("s" * 32, joined)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN=", joined)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", spec.environment)
        self.assertNotIn("dangerously-skip-permissions", joined)
        self.assertEqual(
            {argument for argument in spec.argv if argument.startswith("type=bind,")},
            {
                "type=bind,src=/state/handover-broker/one,dst=/run/agent-handover,ro=true",
                "type=bind,src=/state/workspaces/agent-container,dst=/workspace",
                "type=bind,src=/state/projects/agent-container/claude-config,dst=/home/agent/.claude",
                "type=bind,src=/state/shared-auth/claude/oauth-token,dst=/run/secrets/claude-oauth-token,ro=true",
                "type=bind,src=/state/projects/agent-container/cache,dst=/home/agent/.cache",
                "type=bind,src=/state/gh,dst=/home/agent/gh-config,ro=true",
                "type=bind,src=/vault/handovers/agent-container,dst=/handovers/agent-container,ro=true",
            },
        )

    def test_claude_handover_project_rejects_any_writable_mount_overlap(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        overlapping_projects = (
            ("same-state-root", Path("/state")),
            ("ancestor-state-root", Path("/")),
            ("same-workspace", Path("/state/workspaces/agent-container")),
            ("ancestor-workspace", Path("/state/workspaces")),
            (
                "descendant-workspace",
                Path("/state/workspaces/agent-container/handovers/agent-container"),
            ),
            (
                "same-claude-config",
                Path("/state/projects/agent-container/claude-config"),
            ),
            (
                "descendant-claude-config",
                Path(
                    "/state/projects/agent-container/claude-config/"
                    "handovers/agent-container"
                ),
            ),
            ("same-cache", Path("/state/projects/agent-container/cache")),
            (
                "descendant-cache",
                Path(
                    "/state/projects/agent-container/cache/handovers/agent-container"
                ),
            ),
            (
                "inside-claude-auth",
                Path("/state/shared-auth/claude/agent-container"),
            ),
            (
                "inside-github-broker",
                Path("/state/github-broker/agent-container"),
            ),
            (
                "inside-handover-broker",
                Path("/state/handover-broker/agent-container"),
            ),
        )

        for direction, handover_project in overlapping_projects:
            with self.subTest(direction=direction):
                with self.assertRaisesRegex(ValueError, "overlap"):
                    run_claude_spec(
                        layout,
                        handover_project,
                        IMAGE,
                        os.getuid(),
                        os.getgid(),
                        HANDOVER_BROKER,
                    )

    def test_claude_run_layers_private_home_tmpfs_before_nested_mounts(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")

        spec = run_claude_spec(
            layout=layout,
            handover_project=Path("/vault/handovers/agent-container"),
            image=IMAGE,
            uid=os.getuid(),
            gid=os.getgid(),
            handover_broker=HANDOVER_BROKER,
        )

        home_tmpfs = (
            "type=tmpfs,dst=/home/agent,tmpfs-size=16777216,"
            "tmpfs-mode=0700,U=true,noexec,nosuid,nodev"
        )
        self.assertIn(home_tmpfs, spec.argv)
        home_index = spec.argv.index(home_tmpfs)
        self.assertEqual(spec.argv[home_index - 1], "--mount")
        self.assertFalse(
            any(argument.startswith("--tmpfs=/home/agent:") for argument in spec.argv)
        )
        nested_mounts = (
            "type=bind,src=/state/projects/agent-container/claude-config,"
            "dst=/home/agent/.claude",
            "type=bind,src=/state/projects/agent-container/cache,"
            "dst=/home/agent/.cache",
            "type=bind,src=/state/gh,dst=/home/agent/gh-config,ro=true",
        )
        self.assertTrue(
            all(home_index < spec.argv.index(mount) for mount in nested_mounts)
        )

    def test_claude_run_rejects_uid_or_gid_other_than_current_process(self) -> None:
        layout = StateLayout(Path("/state"), "agent-container")
        handover_project = Path("/vault/handovers/agent-container")
        with self.assertRaisesRegex(ValueError, "current user"):
            run_claude_spec(
                layout,
                handover_project,
                IMAGE,
                os.getuid() + 1,
                os.getgid(),
                HANDOVER_BROKER,
            )
        with self.assertRaisesRegex(ValueError, "current user"):
            run_claude_spec(
                layout,
                handover_project,
                IMAGE,
                os.getuid(),
                os.getgid() + 1,
                HANDOVER_BROKER,
            )


if __name__ == "__main__":
    unittest.main()
