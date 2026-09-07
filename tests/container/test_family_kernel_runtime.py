"""Accept/stop boundaries that Family retains around shared iteration."""

import os
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_container.family_intake_broker import FamilyIntakeDenied
from agent_container.family_intake_runtime import FamilyIntakeRuntime
from agent_container.family_state import FamilyStateLayout


class Stream:
    def __init__(self, events):
        self.events = events

    def close(self):
        self.events.append("stream-close")


class Client:
    def __init__(self, events, *, pid=4242):
        self.events = events
        self.closed = 0
        self.timeout = None
        self.pid = pid
        self.stream = Stream(events)

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

    def getsockopt(self, _level, _option, _size):
        self.events.append("peercred")
        return struct.pack("3i", self.pid, os.getuid(), 0)

    def makefile(self, *_args, **_kwargs):
        self.events.append("makefile")
        return self.stream

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

    def validate_peer(peer_pid, peer_uid):
        if peer_pid == 1:
            raise FamilyIntakeDenied()

    runtime.session = SimpleNamespace(
        consumed=consumed, failed=failed, validate_peer=validate_peer
    )
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
            self.assertIs(observed.client, client)
            self.assertIs(observed.stream, client.stream)
            self.assertEqual((observed.peer_pid, observed.peer_uid), (4242, os.getuid()))
            self.assertIs(runtime._client, client)
            self.assertIs(session, runtime.session)
            self.assertEqual(store, runtime.layout.family_pending_dir)
            events.append("handle")

        with patch("agent_container.family_intake_runtime.handle_family_intake_connection", handle):
            runtime._serve(listener)
        self.assertEqual(events, ["accept", "accept", "enter", "timeout", "peercred", "makefile", "handle", "stream-close", "exit", "client-close", "listener-close"])
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
                self.assertEqual(events, ["accept", "enter", "timeout", "peercred", "makefile", "handle", "stream-close", "exit", "client-close", "listener-close"])
                self.assertTrue(runtime._stop.is_set())
                self.assertTrue(runtime._error)
                self.assertIsNone(runtime._client)
                self.assertEqual(client.closed, 1)

    def test_denied_peer_is_closed_unread_and_the_loop_continues(self):
        events = []
        denied = Client(events, pid=1)
        stopping = Client(events)
        listener = Listener(events, ())
        runtime = make_runtime(listener)

        def stopping_accept():
            runtime._stop.set()
            return stopping

        listener.actions = iter((denied, stopping_accept))

        def handle(*_):
            raise AssertionError("denied peer reached the handler")

        with patch("agent_container.family_intake_runtime.handle_family_intake_connection", handle):
            runtime._serve(listener)
        self.assertEqual(events, [
            "accept", "enter", "timeout", "peercred", "makefile", "stream-close",
            "exit", "client-close", "accept", "client-close",
        ])
        self.assertEqual(denied.timeout, 30)
        self.assertIsNone(runtime._client)
        self.assertFalse(runtime._error)
        self.assertEqual(listener.closed, 0)
