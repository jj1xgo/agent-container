import os
import unittest

from agent_container.broker.peer import PeerPolicy
from agent_container.broker.peer import SameUser
from agent_container.broker.runtime import Connection
from agent_container.broker.runtime import admit_connection
from tests.container.test_broker_runtime import FakeClient


class Deny:
    def admit(self, connection: Connection) -> bool:
        return False


class Explode:
    def admit(self, connection: Connection) -> bool:
        raise RuntimeError("private-policy-marker")


class PeerPolicyTest(unittest.TestCase):
    def test_same_user_admits_only_the_running_user(self) -> None:
        policy: PeerPolicy = SameUser()
        own = Connection(None, None, os.getuid(), 1, 1)
        other = Connection(None, None, os.getuid() + 1, 1, 1)
        self.assertTrue(policy.admit(own))
        self.assertFalse(policy.admit(other))

    def test_admit_connection_keeps_all_peer_credentials(self) -> None:
        client = FakeClient(4040)
        connection = admit_connection(client, timeout=7, policy=None)
        self.assertEqual(connection, Connection(client, client.stream, 4040, 1234, 5678))
        self.assertEqual(client.timeout, 7)
        self.assertFalse(client.stream.closed)

    def test_denied_connection_is_closed_without_reading_or_writing(self) -> None:
        client = FakeClient(os.getuid())
        self.assertIsNone(admit_connection(client, timeout=7, policy=Deny()))
        self.assertTrue(client.stream.closed)
        self.assertEqual(client.stream.outgoing.getvalue(), b"")

    def test_policy_failure_closes_the_stream_and_propagates(self) -> None:
        client = FakeClient(os.getuid())
        with self.assertRaisesRegex(RuntimeError, "private-policy-marker"):
            admit_connection(client, timeout=7, policy=Explode())
        self.assertTrue(client.stream.closed)
