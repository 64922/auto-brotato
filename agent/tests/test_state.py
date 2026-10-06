"""state.py 单元测试：链路健康、频率与摘要格式化。"""
import unittest

from ab_agent.state import AgentState


def snapshot(wave=None, player=None):
    return {"t": 1.0, "wave": wave, "player": player}


class SnapshotHzTest(unittest.TestCase):
    def test_no_samples(self):
        self.assertEqual(AgentState().snapshot_hz(), 0.0)

    def test_single_sample(self):
        state = AgentState()
        state.update_snapshot(snapshot(), now=100.0)
        self.assertEqual(state.snapshot_hz(), 0.0)

    def test_steady_60hz(self):
        state = AgentState()
        for index in range(60):
            state.update_snapshot(snapshot(), now=100.0 + index / 60.0)
        self.assertAlmostEqual(state.snapshot_hz(), 60.0, delta=1.0)


class LinkHealthTest(unittest.TestCase):
    def test_not_connected_is_unhealthy(self):
        state = AgentState()
        state.update_snapshot(snapshot(), now=100.0)
        self.assertFalse(state.link_healthy(now=100.1))

    def test_connected_and_fresh_is_healthy(self):
        state = AgentState()
        state.mark_connected("s1", {})
        state.update_snapshot(snapshot(), now=100.0)
        self.assertTrue(state.link_healthy(now=100.5))

    def test_stale_snapshot_is_unhealthy(self):
        state = AgentState()
        state.mark_connected("s1", {})
        state.update_snapshot(snapshot(), now=100.0)
        self.assertFalse(state.link_healthy(now=103.0))

    def test_snapshot_age_none_before_first(self):
        self.assertIsNone(AgentState().snapshot_age(now=1.0))


class ObserveOnlyTest(unittest.TestCase):
    def test_default_observe_only(self):
        self.assertTrue(AgentState().observe_only)

    def test_first_connect_takes_control(self):
        state = AgentState()
        state.mark_connected("s1", {})
        self.assertFalse(state.observe_only, "本进程首次连接应直接接管（PRD 主流程）")

    def test_reconnect_restores_observe_only(self):
        state = AgentState()
        state.mark_connected("s1", {})
        state.mark_disconnected()
        state.mark_connected("s2", {})
        self.assertTrue(state.observe_only)
        self.assertEqual(state.session_id, "s2")


class MenuStateTest(unittest.TestCase):
    def test_update_menu_and_age(self):
        state = AgentState()
        state.update_menu({"phase": "difficulty_select"}, now=100.0)
        self.assertEqual(state.menu_count, 1)
        self.assertEqual(state.latest_menu["phase"], "difficulty_select")
        self.assertAlmostEqual(state.menu_age(now=101.5), 1.5)

    def test_menu_age_none_before_first(self):
        self.assertIsNone(AgentState().menu_age(now=1.0))


class SummaryTest(unittest.TestCase):
    def test_no_snapshot(self):
        self.assertEqual(AgentState().wave_summary(), "无快照")

    def test_wave_and_player(self):
        state = AgentState()
        state.update_snapshot(
            snapshot(
                wave={"index": 3, "phase": "combat", "time_left": 12.4},
                player={"alive": True, "hp": 18.0},
            )
        )
        summary = state.wave_summary()
        self.assertIn("第 3 波", summary)
        self.assertIn("combat", summary)
        self.assertIn("玩家存活", summary)

    def test_outside_run(self):
        state = AgentState()
        state.update_snapshot(snapshot(wave=None, player=None))
        self.assertIn("未在对局", state.wave_summary())

    def test_describe_contains_core_fields(self):
        state = AgentState()
        state.mark_connected("s1", {})
        state.update_snapshot(snapshot(wave={"index": 1, "phase": "combat", "time_left": 5}), now=1.0)
        text = state.describe(now=1.02)
        self.assertIn("已连接", text)
        self.assertIn("接管", text)
        self.assertIn("快照=", text)
        self.assertIn("第 1 波", text)


if __name__ == "__main__":
    unittest.main()
