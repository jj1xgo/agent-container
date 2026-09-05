from pathlib import Path
import threading
import unittest

from agent_container.egress_broker_runtime import EgressBrokerRuntime


class FakeListener:
    def __init__(self) -> None:
        self.timeout: float | None = None
        self.closed = False
        self._wake = threading.Event()

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def accept(self):
        self._wake.wait(0.01)
        raise TimeoutError

    def close(self) -> None:
        self.closed = True
        self._wake.set()


class FakeSession:
    def __init__(self, listener: FakeListener) -> None:
        self.run_dir = Path("/private/runtime")
        self.project_id = "demo"
        self.agent = "codex"
        self.listener = listener
        self.backlogs = []
        self.deactivate_calls = 0
        self.close_calls = 0

    def open_listener(self, backlog: int = 4) -> FakeListener:
        self.backlogs.append(backlog)
        return self.listener

    def deactivate(self) -> None:
        self.deactivate_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class EgressBrokerRuntimeSurfaceTest(unittest.TestCase):
    def test_listener_and_thread_follow_the_runtime_lifecycle(self) -> None:
        listener = FakeListener()
        session = FakeSession(listener)
        runtime = EgressBrokerRuntime(session)  # type: ignore[arg-type]

        self.assertIsNone(runtime._listener)
        self.assertIsNone(runtime._thread)

        with runtime:
            self.assertIs(runtime._listener, listener)
            self.assertTrue(runtime._thread.is_alive())  # type: ignore[union-attr]

        self.assertIsNone(runtime._listener)
        self.assertTrue(listener.closed)

    def test_private_surface_used_by_integration_tests_exists(self) -> None:
        # This private surface is relied on by tests outside this module:
        # tests/integration/test_egress_podman.py (Podman-only, not run here)
        # asserts on `_listener` directly, and
        # tests/container/test_egress_broker_runtime.py exercises the rest
        # (`_thread`, `_handle_client`, `_reserve_tunnel`, `_mark_tunnel_created`,
        # `_release_tunnel`, `_created_tunnels`, `_active_reservations`). This
        # test is a fast guard so a future refactor of EgressBrokerRuntime
        # cannot silently drop any of it.
        runtime = EgressBrokerRuntime(FakeSession(FakeListener()))  # type: ignore[arg-type]

        for name in (
            "_thread",
            "_listener",
            "_handle_client",
            "_reserve_tunnel",
            "_mark_tunnel_created",
            "_release_tunnel",
            "_created_tunnels",
            "_active_reservations",
        ):
            self.assertTrue(hasattr(runtime, name), f"missing {name}")


if __name__ == "__main__":
    unittest.main()
