import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest import mock

from agent_container.egress_runtime import forward_signal
from agent_container.egress_runtime import run
from agent_container.egress_runtime import self_check
from agent_container.egress_runtime import validate_agent_command


class _Process:
    def __init__(self, polls: list[int | None]) -> None:
        self.polls = polls
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.signals: list[int] = []

    def poll(self) -> int | None:
        if self.returncode is None and self.polls:
            value = self.polls.pop(0)
            if value is not None:
                self.returncode = value
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = -signal.SIGTERM if self.terminated else 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -signal.SIGKILL

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)


class EgressRuntimeTest(unittest.TestCase):
    def test_accepts_only_fixed_nonempty_agent_commands(self) -> None:
        self.assertEqual(validate_agent_command(["codex", "--help"]), ["codex", "--help"])
        self.assertEqual(
            validate_agent_command(
                ["python3", "-m", "agent_container.claude_launcher", "--help"]
            ),
            ["python3", "-m", "agent_container.claude_launcher", "--help"],
        )
        for command in (
            [],
            ["sh", "-c", "codex"],
            ["python3", "agent.py"],
            ["python3", "-m", "other.module"],
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                validate_agent_command(command)

    def test_self_check_requires_both_managed_executables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = root / "agent-egress-adapter"
            runtime = root / "agent-egress-runtime"
            adapter.write_text("#!/bin/sh\n", encoding="ascii")
            runtime.write_text("#!/bin/sh\n", encoding="ascii")
            adapter.chmod(0o755)
            runtime.chmod(0o755)
            self.assertEqual(self_check(adapter, runtime), 0)
            adapter.write_text("#!/bin/sh\nexit 9\n", encoding="ascii")
            self.assertEqual(self_check(adapter, runtime), 1)
            adapter.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
            adapter.chmod(0o644)
            self.assertEqual(self_check(adapter, runtime), 1)

    def test_starts_adapter_waits_for_readiness_then_starts_agent_without_shell(self) -> None:
        adapter = _Process([None, None, None])
        child = _Process([7])
        calls: list[tuple[list[str], dict[str, object]]] = []

        def launch(argv: list[str], **kwargs: object) -> _Process:
            calls.append((argv, kwargs))
            if len(calls) == 1:
                ready_fd = kwargs["pass_fds"][0]
                os.write(ready_fd, b"ready\n")
                return adapter
            return child

        result = run(
            ["--", "codex", "--help"],
            launcher=launch,
            poll_interval=0,
        )

        self.assertEqual(result, 7)
        self.assertEqual(calls[0][0][0], "/usr/local/bin/agent-egress-adapter")
        self.assertEqual(calls[1][0], ["codex", "--help"])
        self.assertNotIn("shell", calls[0][1])
        self.assertNotIn("shell", calls[1][1])
        self.assertTrue(adapter.terminated)

    def test_unexpected_adapter_death_terminates_agent_and_returns_nonzero(self) -> None:
        adapter = _Process([None, 0])
        child = _Process([None, None])
        calls = 0

        def launch(_argv: list[str], **kwargs: object) -> _Process:
            nonlocal calls
            calls += 1
            if calls == 1:
                os.write(kwargs["pass_fds"][0], b"ready\n")
                return adapter
            return child

        result = run(["--", "codex"], launcher=launch, poll_interval=0)

        self.assertEqual(result, 1)
        self.assertTrue(child.terminated)

    def test_forwards_signals_to_both_live_children(self) -> None:
        adapter = _Process([None])
        child = _Process([None])
        forward_signal((adapter, child), signal.SIGINT)
        self.assertEqual(adapter.signals, [signal.SIGINT])
        self.assertEqual(child.signals, [signal.SIGINT])

    def test_installs_signal_forwarding_before_adapter_readiness(self) -> None:
        adapter = _Process([None, None])
        child = _Process([0])
        handlers: dict[int, object] = {}
        calls = 0

        def install(signum: int, handler: object) -> object:
            handlers[signum] = handler
            return signal.SIG_DFL

        def launch(_argv: list[str], **kwargs: object) -> _Process:
            nonlocal calls
            calls += 1
            if calls == 1:
                self.assertIn(signal.SIGTERM, handlers)
                os.write(kwargs["pass_fds"][0], b"ready\n")
                return adapter
            return child

        def wait_ready(readers, _writers, _errors, _timeout):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return readers, [], []

        with mock.patch("agent_container.egress_runtime.signal.signal", side_effect=install), mock.patch(
            "agent_container.egress_runtime.signal.getsignal", return_value=signal.SIG_DFL
        ), mock.patch(
            "agent_container.egress_runtime.select.select", side_effect=wait_ready
        ):
            self.assertEqual(
                run(["--", "codex"], launcher=launch, poll_interval=0),
                128 + signal.SIGTERM,
            )
        self.assertEqual(adapter.signals, [signal.SIGTERM])


if __name__ == "__main__":
    unittest.main()
