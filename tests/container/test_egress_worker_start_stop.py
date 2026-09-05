"""Release-line regression for a registered worker whose start is pending."""

import threading
import unittest
from unittest import mock

from agent_container.egress_broker_runtime import EgressBrokerRuntime
from agent_container.egress_broker_runtime import EgressBrokerRuntimeError
from tests.container.test_egress_broker_runtime import FakeClient
from tests.container.test_egress_broker_runtime import FakeListener
from tests.container.test_egress_broker_runtime import FakeStream
from tests.container.test_egress_broker_runtime import ProcessingSession


class EgressWorkerStartStopTest(unittest.TestCase):
    def test_pending_start_is_retained_and_reclaimed_on_retry(self):
        self._check_pending_start(start_error=None)

    def test_start_failure_is_reclaimed_and_reported_on_retry(self):
        self._check_pending_start(start_error=OSError("start-marker"))

    def _check_pending_start(self, *, start_error):
        starting = threading.Event()
        release = threading.Event()
        client = FakeClient(FakeStream(b""))
        session = ProcessingSession()
        session.listener = FakeListener((client,))
        runtime = EgressBrokerRuntime(session)

        class PausedThread(threading.Thread):
            def start(self):
                if self.name == "egress-tunnel":
                    starting.set()
                    if not release.wait(5):
                        raise RuntimeError("test start barrier timed out")
                    if start_error is not None:
                        raise start_error
                super().start()

        first_error = None
        retry_error = None
        with mock.patch("threading.Thread", PausedThread):
            runtime.__enter__()
            try:
                self.assertTrue(starting.wait(5))
                with runtime._worker_lock:
                    workers = tuple(runtime._workers)
                self.assertEqual(len(workers), 1)
                self.assertIsNone(workers[0].ident)
                with mock.patch(
                    "agent_container.egress_broker_runtime._STOP_TIMEOUT_SECONDS", 0
                ):
                    try:
                        runtime.__exit__()
                    except Exception as error:
                        first_error = error
                stopped_state = (
                    session.deactivate_calls, session.close_calls, runtime._exited
                )
                with runtime._worker_lock:
                    retained = tuple(runtime._workers)
            finally:
                release.set()
                try:
                    runtime.__exit__()
                except EgressBrokerRuntimeError as error:
                    retry_error = error

        self.assertIsInstance(first_error, EgressBrokerRuntimeError)
        self.assertEqual(str(first_error), "egress broker did not stop")
        self.assertEqual(stopped_state, (1, 0, False))
        self.assertEqual(retained, workers)
        if start_error is None:
            self.assertIsNone(retry_error)
            self.assertIsNone(runtime._error)
            self.assertTrue(client.stream.closed)
        else:
            self.assertIsInstance(retry_error, EgressBrokerRuntimeError)
            self.assertEqual(str(retry_error), "egress broker failed")
            self.assertIs(runtime._error, start_error)
        self.assertTrue(runtime._exited)
        self.assertTrue(client.closed)
        self.assertFalse(runtime._thread.is_alive())
        self.assertFalse(workers[0].is_alive())
        self.assertEqual(runtime._workers, set())
        self.assertEqual(session.close_calls, 1)
        runtime.__exit__()
        self.assertEqual(session.close_calls, 1)
