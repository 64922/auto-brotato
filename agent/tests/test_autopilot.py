"""autopilot.py 单元测试：占位走位的节流与贴边修正。"""
import random
import unittest

from ab_agent.autopilot import PlaceholderAutopilot

from tests import payloads


class PlaceholderAutopilotTest(unittest.TestCase):
    def test_throttles_by_interval(self):
        autopilot = PlaceholderAutopilot(rng=random.Random(1), move_interval_s=0.1)
        first = autopilot.next_move(payloads.snapshot(), now=0.0)
        self.assertIsNotNone(first)
        self.assertIsNone(autopilot.next_move(payloads.snapshot(), now=0.05))
        self.assertIsNotNone(autopilot.next_move(payloads.snapshot(), now=0.11))

    def test_vector_is_bounded(self):
        autopilot = PlaceholderAutopilot(rng=random.Random(2), move_interval_s=0.0)
        for _ in range(20):
            vector = autopilot.next_move(payloads.snapshot(), now=1.0)
            self.assertLessEqual(max(abs(vector[0]), abs(vector[1])), 0.7)
            self.assertNotEqual(vector, [0.0, 0.0])

    def test_near_edge_steers_inward(self):
        autopilot = PlaceholderAutopilot(rng=random.Random(3), move_interval_s=0.0)
        snapshot = payloads.snapshot()
        snapshot["player"]["pos"] = [10.0, 10.0]
        snapshot["arena"] = {"min": [0.0, 0.0], "max": [1000.0, 800.0]}
        for _ in range(20):
            vector = autopilot.next_move(snapshot, now=2.0)
            self.assertGreaterEqual(vector[0], 0.0)
            self.assertGreaterEqual(vector[1], 0.0)

    def test_reset_clears_throttle(self):
        autopilot = PlaceholderAutopilot(rng=random.Random(4), move_interval_s=10.0)
        self.assertIsNotNone(autopilot.next_move(payloads.snapshot(), now=0.0))
        self.assertIsNone(autopilot.next_move(payloads.snapshot(), now=0.1))
        autopilot.reset()
        self.assertIsNotNone(autopilot.next_move(payloads.snapshot(), now=0.2))


if __name__ == "__main__":
    unittest.main()
