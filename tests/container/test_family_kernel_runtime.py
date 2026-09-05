"""Accept/stop boundaries that Family retains around shared iteration."""

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_container.family_intake_runtime import FamilyIntakeRuntime
from agent_container.family_state import FamilyStateLayout


class Client:
    def __init__(self, events):
        self.events = events
        self.closed = 0
        self.timeout = None

    def __enter__(self):
        self.events.append("enter")
        return self

    def __exit__(self, *_):
        self.events.append("exit")
        self.close()

    def close(self):
        self.closed += 1
        self.events.append("client-close")

    def settimeout(self, timeout):
        self.timeout = timeout
        self.events.append("timeout")

    def shutdown(self, _how):
        self.events.append("shutdown")


class Listener:
    def __init__(self, events, actions):
        self.events = events
        self.actions = iter(actions)
        self.closed = 0
        self.accepted = 0

    def accept(self):
        self.accepted += 1
        self.events.append("accept")
        action = next(self.actions)
        if callable(action):
            action = action()
        if isinstance(action, Exception):
            raise action
        return action, None

    def close(self):
        self.closed += 1
        self.events.append("listener-close")


def make_runtime(listener, *, consumed=False, failed=False):
    runtime = FamilyIntakeRuntime(
        FamilyStateLayout(Path("/synthetic/state"), "demo"),
        agent="codex", repository="demo",
    )
    runtime.session = SimpleNamespace(consumed=consumed, failed=failed)
    runtime._listener = listener
    return runtime


class FamilyKernelRuntimeTest(unittest.TestCase):
    def test_pre_stopped_loop_does_not_accept(self):
        listener = Listener([], ())
        runtime = make_runtime(listener)
        runtime._stop.set()
        runtime._serve(listener)
        self.assertEqual(listener.accepted, 0)
        self.assertFalse(runtime._error)

    def test_timeout_then_consumed_request_closes_client_before_listener(self):
        events = []
        client = Client(events)
        listener = Listener(events, (TimeoutError(), client))
        runtime = make_runtime(listener, consumed=True)

        def handle(observed, session, store):
            self.assertIs(observed, client)
            self.assertIs(runtime._client, client)
            self.assertIs(session, runtime.session)
            self.assertEqual(store, runtime.layout.family_pending_dir)
            events.append("handle")

        with patch("agent_container.family_intake_runtime.handle_family_intake_connection", handle):
            runtime._serve(listener)
        self.assertEqual(events, ["accept", "accept", "enter", "timeout", "handle", "exit", "client-close", "listener-close"])
        self.assertEqual(client.timeout, 30)
        self.assertIsNone(runtime._client)
        self.assertTrue(runtime._stop.is_set())
        self.assertFalse(runtime._error)

    def test_stop_during_accept_closes_unowned_client_without_handling(self):
        events = []
        client = Client(events)
        listener = Listener(events, ())
        runtime = make_runtime(listener)

        def stopping_accept():
            runtime._stop.set()
            return client

        listener.actions = iter((stopping_accept,))
        runtime._serve(listener)
        self.assertEqual(events, ["accept", "client-close"])
        self.assertEqual(client.closed, 1)
        self.assertIsNone(runtime._client)
        self.assertFalse(runtime._error)

    def test_accept_oserror_fails_active_runtime_but_not_stopped_runtime(self):
        for stopped in (False, True):
            with self.subTest(stopped=stopped):
                events = []
                listener = Listener(events, ())
                runtime = make_runtime(listener)

                def failing_accept():
                    if stopped:
                        runtime._stop.set()
                    raise OSError("private accept marker")

                listener.actions = iter((failing_accept,))
                runtime._serve(listener)
                self.assertEqual(runtime._error, not stopped)
                self.assertTrue(runtime._stop.is_set())
                self.assertEqual(listener.closed, 0 if stopped else 1)

    def test_handler_failure_and_failed_session_release_client_before_failure(self):
        for handler_error in (False, True):
            with self.subTest(handler_error=handler_error):
                events = []
                client = Client(events)
                listener = Listener(events, (client,))
                runtime = make_runtime(listener, failed=not handler_error)

                def handle(*_):
                    events.append("handle")
                    if handler_error:
                        raise RuntimeError("private handler marker")

                with patch("agent_container.family_intake_runtime.handle_family_intake_connection", handle):
                    runtime._serve(listener)
                self.assertEqual(events, ["accept", "enter", "timeout", "handle", "exit", "client-close", "listener-close"])
                self.assertTrue(runtime._stop.is_set())
                self.assertTrue(runtime._error)
                self.assertIsNone(runtime._client)
                self.assertEqual(client.closed, 1)
