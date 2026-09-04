import unittest

from agent_container.broker.readiness import AlwaysReady
from agent_container.broker.readiness import ReadinessGate


class AlwaysReadyTest(unittest.TestCase):
    def test_is_a_gate_that_is_always_open(self) -> None:
        gate: ReadinessGate = AlwaysReady()
        self.assertTrue(gate.wait())
        self.assertTrue(gate.wait(timeout=0))


if __name__ == "__main__":
    unittest.main()
