"""Regression for stop racing a registered worker's Thread.start()."""

import threading
import unittest
from unittest import mock

from tests.container.test_broker_runtime import FakeClient
from tests.container.test_broker_runtime import FakeListener
from tests.container.test_broker_runtime import RuntimeError_
from tests.container.test_broker_runtime import make_runtime


class WorkerStartStopTest(unittest.TestCase):
    def test_pending_worker_reports_retryable_stop_and_is_reclaimed_on_retry(self):
        self._check_pending_start(start_error=None)

    def test_pending_worker_start_failure_is_reclaimed_and_reported_on_retry(self):
        self._check_pending_start(start_error=OSError("private-start-marker"))

    def _check_pending_start(self, *, start_error):
        for late_deactivate in (False, True):
            with self.subTest(deactivate_after_join=late_deactivate):
                starting = threading.Event()
                release = threading.Event()
                client = FakeClient(1010)
                listener = FakeListener((client,))
                runtime, calls = make_runtime(
                    listener,
                    concurrency="thread",
                    worker_thread_name="paused-worker",
                    deactivate_after_join=late_deactivate,
                )

                class PausedWorkerThread(threading.Thread):
                    def start(self):
                        if self.name == "paused-worker":
                            starting.set()
                            if not release.wait(5):
                                raise RuntimeError("test start barrier timed out")
                            if start_error is not None:
                                raise start_error
                        super().start()

                first_error = None
                retry_error = None
                with mock.patch("threading.Thread", PausedWorkerThread):
                    runtime.start()
                    try:
                        self.assertTrue(starting.wait(5))
                        with runtime.worker_lock:
                            workers = tuple(runtime.workers)
                        self.assertEqual(len(workers), 1)
                        self.assertIsNone(workers[0].ident)
                        try:
                            runtime.stop(join_timeout=0)
                        except Exception as error:
                            first_error = error
                        stopped_state = (
                            calls["deactivate"], calls["close"], runtime.exited
                        )
                    finally:
                        release.set()
                        try:
                            runtime.stop(join_timeout=5)
                        except RuntimeError_ as error:
                            retry_error = error

                self.assertIsInstance(first_error, RuntimeError_)
                self.assertEqual(str(first_error), "test broker did not stop")
                self.assertEqual(stopped_state, (1, 0, False))
                if start_error is None:
                    self.assertIsNone(retry_error)
                    self.assertIsNone(runtime.error)
                    self.assertTrue(client.stream.closed)
                else:
                    self.assertIsInstance(retry_error, RuntimeError_)
                    self.assertEqual(str(retry_error), "test broker failed")
                    self.assertIs(runtime.error, start_error)
                self.assertTrue(runtime.exited)
                self.assertTrue(client.closed)
                self.assertFalse(runtime.thread.is_alive())
                self.assertFalse(workers[0].is_alive())
                self.assertEqual(runtime.workers, set())
                self.assertEqual(calls["close"], 1)
                runtime.stop(join_timeout=0)
                self.assertEqual(calls["close"], 1)


if __name__ == "__main__":
    unittest.main()
