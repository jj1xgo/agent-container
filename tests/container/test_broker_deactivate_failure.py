"""K1: a failing deactivate() still closes the listener, joins, and cleans up."""

import os
import threading
import unittest
from unittest import mock

from tests.container.test_broker_runtime import FakeClient
from tests.container.test_broker_runtime import FakeListener
from tests.container.test_broker_runtime import RuntimeError_
from tests.container.test_broker_runtime import make_runtime


class FlakyDeactivate:
    def __init__(self, failures: int = 1) -> None:
        self.calls = 0
        self.failures = failures

    def __call__(self) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise ValueError("private-deactivate-marker")


class DeactivateFailureTest(unittest.TestCase):
    def test_failure_continues_cleanup_and_is_reported_until_deactivate_succeeds(self) -> None:
        for late, concurrency in (
            (False, "inline"),
            (True, "inline"),
            (False, "thread"),
            (True, "thread"),
        ):
            with self.subTest(deactivate_after_join=late, concurrency=concurrency):
                handled = threading.Event()
                client = FakeClient(os.getuid())
                listener = FakeListener((client,))
                deactivate = FlakyDeactivate()

                def handler(connection) -> int:
                    handled.set()
                    return 0

                runtime, calls = make_runtime(
                    listener,
                    handler,
                    deactivate=deactivate,
                    concurrency=concurrency,
                    raw_client=concurrency == "thread",
                    deactivate_after_join=late,
                )
                runtime.start()
                self.assertTrue(handled.wait(1))

                with self.assertRaises(RuntimeError_) as raised:
                    runtime.stop(join_timeout=2)
                self.assertEqual(str(raised.exception), "test broker deactivate failed")
                self.assertNotIn("private-deactivate-marker", str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertTrue(listener.closed)
                self.assertTrue(client.closed)
                self.assertEqual(deactivate.calls, 1)
                self.assertEqual(calls["close"], 1)
                self.assertFalse(runtime.exited)
                self.assertIsInstance(runtime.deactivate_error, ValueError)

                runtime.stop(join_timeout=2)
                self.assertEqual(deactivate.calls, 2)
                self.assertEqual(calls["close"], 2)
                self.assertTrue(runtime.exited)
                self.assertIsNone(runtime.deactivate_error)

                runtime.stop(join_timeout=2)
                self.assertEqual(calls["close"], 2)

    def test_did_not_stop_outranks_deactivate_failure_and_retry_deactivates_again(self) -> None:
        listener = FakeListener()
        deactivate = FlakyDeactivate()

        class StuckThread:
            daemon = True

            def __init__(self, **_: object) -> None:
                self.alive = True

            def start(self) -> None:
                pass

            def join(self, timeout: float) -> None:
                pass

            def is_alive(self) -> bool:
                return self.alive

        thread = StuckThread()
        runtime, calls = make_runtime(listener, deactivate=deactivate)
        with mock.patch("threading.Thread", return_value=thread):
            runtime.start()
        with self.assertRaises(RuntimeError_) as raised:
            runtime.stop(join_timeout=0)
        self.assertEqual(str(raised.exception), "test broker did not stop")
        self.assertEqual(deactivate.calls, 1)
        self.assertEqual(calls["close"], 0)
        self.assertTrue(listener.closed)

        thread.alive = False
        runtime.stop(join_timeout=0)
        self.assertEqual(deactivate.calls, 2)
        self.assertEqual(calls["close"], 1)
        self.assertTrue(runtime.exited)

    def test_deactivate_failure_outranks_cleanup_failure(self) -> None:
        listener = FakeListener()
        deactivate = FlakyDeactivate(failures=99)

        def failing_close() -> None:
            raise OSError("private-close-marker")

        runtime, _ = make_runtime(listener, deactivate=deactivate, close=failing_close)
        runtime.start()
        with self.assertRaises(RuntimeError_) as raised:
            runtime.stop(join_timeout=2)
        self.assertEqual(str(raised.exception), "test broker deactivate failed")
        self.assertFalse(runtime.exited)

    def test_base_exception_from_deactivate_is_not_swallowed(self) -> None:
        listener = FakeListener()

        def interrupt() -> None:
            raise KeyboardInterrupt

        runtime, calls = make_runtime(listener, deactivate=interrupt)
        runtime.start()
        with self.assertRaises(KeyboardInterrupt):
            runtime.stop(join_timeout=2)
        self.assertFalse(runtime.exited)
        runtime.deactivate = lambda: None
        runtime.stop(join_timeout=2)
        self.assertEqual(calls["close"], 1)
        self.assertTrue(runtime.exited)
