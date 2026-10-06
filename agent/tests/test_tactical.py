"""tactical.py 测试：节拍、波次节奏、材料簇/消耗品/Boss 目标、低血保守模式与接线。"""
import json
import math
import unittest

from ab_agent.cli import build_parser
from ab_agent.decision.config import load_reflex_config
from ab_agent.decision.reflex import ReflexController
from ab_agent.decision.tactical import (
    TARGET_BOSS,
    TARGET_CLUSTER,
    TARGET_CONSUMABLE,
    TARGET_NONE,
    TacticalController,
)
from ab_agent.decision.tactical_config import (
    DEFAULT_CONFIG_PATH,
    tactical_config_from_mapping,
)
from ab_agent.run_control import RunController

ARENA = {"min": [0.0, 0.0], "max": [2048.0, 1536.0]}
CENTER = (1024.0, 768.0)


def make_config(**sections):
    """默认配置 + 分区覆盖（测试专用；生产配置来自 JSON 文件）。"""
    raw = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    for section, values in sections.items():
        raw[section].update(values)
    return tactical_config_from_mapping(raw)


def combat_snapshot(
    *,
    pos=CENTER,
    hp=100.0,
    max_hp=100.0,
    alive=True,
    wave=1,
    enemies=(),
    pickups=(),
    projectiles=(),
    hazards=(),
    arena=ARENA,
    economy=None,
):
    return {
        "t": 0.0,
        "wave": (
            {"index": wave, "phase": "combat", "time_left": 10.0, "time_total": 20.0}
            if wave is not None
            else None
        ),
        "player": {
            "pos": list(pos),
            "vel": [0.0, 0.0],
            "hp": hp,
            "max_hp": max_hp,
            "alive": alive,
            "invuln": False,
        },
        "weapons": [],
        "enemies": [dict(enemy) for enemy in enemies],
        "projectiles": [dict(item) for item in projectiles],
        "pickups": [dict(item) for item in pickups],
        "hazards": [dict(item) for item in hazards],
        "arena": arena,
        "inventory": {"weapons": [], "items": []},
        "economy": economy or {"gold": 0, "materials_this_wave": 0},
        "truncated": False,
    }


def enemy(x, y, *, radius=20.0, boss=False):
    return {
        "id": 1,
        "type": "chaser",
        "pos": [x, y],
        "vel": [0.0, 0.0],
        "hp_pct": 1.0,
        "radius": radius,
        "elite": False,
        "boss": boss,
    }


def pickup(kind, x, y):
    return {"kind": kind, "pos": [x, y]}


def filler_enemies(count):
    """占位敌人（远离玩家），避免触发清场窗口而干扰节奏断言。"""
    return [enemy(1900.0, 1400.0, radius=5.0) for _ in range(count)]


def decide(controller, snapshot, now=1.0):
    """驱动一次战术决策并返回状态（反射输出弃置）。"""
    controller.next_move(snapshot, now=now)
    return controller.state


class ThrottleTest(unittest.TestCase):
    def test_state_refreshes_on_interval_only(self):
        controller = TacticalController()
        first = decide(
            controller, combat_snapshot(pickups=[pickup("material", 1524.0, 768.0)])
        )
        self.assertEqual(first.target, (1524.0, 768.0))
        # 间隔内换目标：状态保持旧值（意图交给反射层持续执行）
        held = controller.next_move(
            combat_snapshot(pickups=[pickup("material", 524.0, 768.0)]), now=1.1
        )
        self.assertEqual(controller.state.target, (1524.0, 768.0))
        self.assertIsNotNone(held)
        # 超过间隔后重算
        controller.next_move(
            combat_snapshot(pickups=[pickup("material", 524.0, 768.0)]), now=1.3
        )
        self.assertEqual(controller.state.target, (524.0, 768.0))

    def test_reset_clears_state(self):
        controller = TacticalController()
        decide(controller, combat_snapshot(pickups=[pickup("material", 1524.0, 768.0)]))
        controller.reset()
        self.assertIsNone(controller.state)
        self.assertIsNotNone(
            controller.next_move(combat_snapshot(), now=0.0)
        )

    def test_skips_without_player_or_dead(self):
        controller = TacticalController()
        snapshot = combat_snapshot()
        snapshot["player"] = None
        self.assertIsNone(controller.next_move(snapshot, now=1.0))
        self.assertIsNone(
            controller.next_move(combat_snapshot(alive=False), now=1.0)
        )


class RhythmTest(unittest.TestCase):
    def test_wave_phases_map_to_scales(self):
        early = decide(TacticalController(), combat_snapshot(wave=1, enemies=filler_enemies(6)))
        mid = decide(TacticalController(), combat_snapshot(wave=10, enemies=filler_enemies(6)))
        late = decide(TacticalController(), combat_snapshot(wave=16, enemies=filler_enemies(6)))
        self.assertEqual(early.rhythm, "early")
        self.assertEqual(mid.rhythm, "mid")
        self.assertEqual(late.rhythm, "late")
        self.assertLess(early.danger_scale, mid.danger_scale)
        self.assertLess(mid.danger_scale, late.danger_scale)
        self.assertGreater(early.attraction_scale, late.attraction_scale)

    def test_missing_wave_defaults_early(self):
        state = decide(TacticalController(), combat_snapshot(wave=None, enemies=filler_enemies(6)))
        self.assertEqual(state.rhythm, "early")

    def test_clear_window_prefers_pickups(self):
        cleared = decide(
            TacticalController(),
            combat_snapshot(
                wave=10, enemies=filler_enemies(2), pickups=[pickup("material", 1324.0, 768.0)]
            ),
        )
        busy = decide(
            TacticalController(), combat_snapshot(wave=10, enemies=filler_enemies(6))
        )
        self.assertTrue(cleared.pickup_window)
        self.assertGreater(cleared.attraction_scale, busy.attraction_scale)
        self.assertLess(cleared.danger_scale, busy.danger_scale)


class ClusterTest(unittest.TestCase):
    def test_prefers_richer_cluster_over_nearest_single(self):
        snapshot = combat_snapshot(
            pickups=[
                pickup("material", 924.0, 768.0),
                pickup("material", 1274.0, 768.0),
                pickup("material", 1294.0, 780.0),
                pickup("material", 1254.0, 756.0),
            ]
        )
        state = decide(TacticalController(), snapshot)
        self.assertEqual(state.target_kind, TARGET_CLUSTER)
        self.assertAlmostEqual(state.target[0], 1274.0, delta=30.0)
        self.assertAlmostEqual(state.target[1], 768.0, delta=30.0)

    def test_nearest_single_wins_between_singles(self):
        state = decide(
            TacticalController(),
            combat_snapshot(
                pickups=[pickup("material", 524.0, 768.0), pickup("material", 1324.0, 768.0)]
            ),
        )
        self.assertAlmostEqual(state.target[0], 1324.0)  # 距玩家 300px，另一颗 500px
        self.assertAlmostEqual(state.target[1], 768.0)

    def test_danger_penalty_avoids_guarded_cluster(self):
        controller = TacticalController(make_config(cluster={"danger_weight": 5.0}))
        snapshot = combat_snapshot(
            enemies=[enemy(1200.0, 768.0)],
            pickups=[
                pickup("material", 1160.0, 768.0),
                pickup("material", 1180.0, 768.0),
                pickup("material", 400.0, 768.0),
                pickup("material", 420.0, 768.0),
            ],
        )
        state = decide(controller, snapshot)
        self.assertAlmostEqual(state.target[0], 410.0, delta=30.0)

    def test_far_materials_out_of_range(self):
        state = decide(
            TacticalController(),
            combat_snapshot(pickups=[pickup("material", 1900.0, 768.0)]),
        )
        self.assertEqual(state.target_kind, TARGET_NONE)
        self.assertIsNone(state.target)

    def test_no_materials_keeps_empty_intent(self):
        state = decide(TacticalController(), combat_snapshot(enemies=filler_enemies(6)))
        self.assertEqual(state.target_kind, TARGET_NONE)
        controller = TacticalController()
        controller.next_move(combat_snapshot(enemies=filler_enemies(6)), now=1.0)
        self.assertIsNone(controller._intent.expected_position)


class ConsumableTest(unittest.TestCase):
    def test_low_hp_prefers_consumable_over_closer_material(self):
        state = decide(
            TacticalController(),
            combat_snapshot(
                hp=55.0,
                pickups=[
                    pickup("consumable", 1224.0, 768.0),
                    pickup("material", 824.0, 768.0),
                ],
            ),
        )
        self.assertEqual(state.target_kind, TARGET_CONSUMABLE)
        self.assertEqual(state.target, (1224.0, 768.0))
        self.assertGreaterEqual(state.attraction_scale, 1.4)

    def test_healthy_player_ignores_consumable(self):
        state = decide(
            TacticalController(),
            combat_snapshot(
                hp=90.0,
                pickups=[
                    pickup("consumable", 1224.0, 768.0),
                    pickup("material", 824.0, 768.0),
                ],
            ),
        )
        self.assertEqual(state.target_kind, TARGET_CLUSTER)
        self.assertEqual(state.target, (824.0, 768.0))

    def test_consumable_out_of_range_falls_back(self):
        state = decide(
            TacticalController(),
            combat_snapshot(
                hp=40.0,
                pickups=[
                    pickup("consumable", 1900.0, 768.0),
                    pickup("material", 824.0, 768.0),
                ],
            ),
        )
        self.assertEqual(state.target_kind, TARGET_CLUSTER)


class ConservativeTest(unittest.TestCase):
    def test_enter_on_low_hp_and_exit_with_hysteresis(self):
        controller = TacticalController()
        normal = decide(controller, combat_snapshot(hp=60.0), now=1.0)
        self.assertFalse(normal.conservative)
        entered = decide(controller, combat_snapshot(hp=40.0), now=1.3)
        self.assertTrue(entered.conservative)
        still = decide(controller, combat_snapshot(hp=60.0), now=1.6)
        self.assertTrue(still.conservative)
        exited = decide(controller, combat_snapshot(hp=70.0), now=1.9)
        self.assertFalse(exited.conservative)

    def test_conservative_raises_danger_and_drops_far_material(self):
        controller = TacticalController()
        near = combat_snapshot(
            hp=40.0, pickups=[pickup("material", 1224.0, 768.0)]
        )
        near_state = decide(controller, near)
        controller.reset()
        far = combat_snapshot(
            hp=40.0, pickups=[pickup("material", 1524.0, 768.0)]
        )
        far_state = decide(controller, far)
        self.assertEqual(near_state.target_kind, TARGET_CLUSTER)
        self.assertEqual(far_state.target_kind, TARGET_NONE)  # 300px 外放弃
        self.assertGreater(near_state.danger_scale, 0.9)  # 早期 0.9 × 1.35
        self.assertLess(near_state.attraction_scale, 1.3)

    def test_conservative_keeps_consumable_attraction(self):
        state = decide(
            TacticalController(),
            combat_snapshot(
                hp=40.0,
                enemies=filler_enemies(6),
                pickups=[pickup("consumable", 1224.0, 768.0)],
            ),
        )
        self.assertEqual(state.target_kind, TARGET_CONSUMABLE)
        self.assertAlmostEqual(state.attraction_scale, 1.4)  # 不乘保守压低系数
        self.assertAlmostEqual(state.danger_scale, 0.9 * 1.35)

    def test_hp_missing_keeps_mode(self):
        controller = TacticalController()
        snapshot = combat_snapshot(hp=40.0)
        decide(controller, snapshot)
        self.assertTrue(controller.conservative)
        snapshot["player"]["max_hp"] = None
        decide(controller, snapshot, now=1.3)
        self.assertTrue(controller.conservative)


class BossTest(unittest.TestCase):
    def test_boss_sets_band_and_ring_target(self):
        state = decide(
            TacticalController(),
            combat_snapshot(wave=20, enemies=[enemy(1024.0, 768.0, boss=True)]),
        )
        self.assertTrue(state.boss)
        self.assertEqual(state.target_kind, TARGET_BOSS)
        self.assertEqual(state.distance_band, (380.0, 620.0))
        self.assertAlmostEqual(
            math.hypot(state.target[0] - 1024.0, state.target[1] - 768.0),
            500.0,
            delta=1.0,
        )
        self.assertFalse(state.pickup_window)

    def test_boss_target_prefers_open_direction_away_from_edge(self):
        config = make_config(boss={"edge_margin": 300.0})
        controller = TacticalController(config)
        state = decide(
            controller,
            combat_snapshot(wave=20, enemies=[enemy(150.0, 150.0, boss=True)]),
        )
        self.assertGreater(state.target[0], 150.0)
        self.assertGreater(state.target[1], 150.0)
        self.assertLess(state.target[0], 2048.0)
        self.assertLess(state.target[1], 1536.0)

    def test_consumable_priority_over_boss(self):
        state = decide(
            TacticalController(),
            combat_snapshot(
                wave=20,
                hp=40.0,
                enemies=[enemy(1024.0, 768.0, boss=True)],
                pickups=[pickup("consumable", 1224.0, 768.0)],
            ),
        )
        self.assertEqual(state.target_kind, TARGET_CONSUMABLE)


class WiringTest(unittest.TestCase):
    def test_run_controller_defaults_to_tactical(self):
        controller = RunController(None, clock=lambda: 0.0, ack_timeout_s=1.0)
        self.assertEqual(controller.autopilot_name, "TacticalController")

    def test_cli_defaults_to_tactical(self):
        args = build_parser().parse_args([])
        self.assertEqual(args.move_controller, "tactical")
        self.assertIsNone(args.tactical_config)

    def test_tactical_wraps_injected_reflex(self):
        reflex = ReflexController(load_reflex_config())
        controller = TacticalController(reflex=reflex)
        self.assertIs(controller._reflex, reflex)

    def test_distance_band_only_set_for_boss(self):
        state = decide(
            TacticalController(),
            combat_snapshot(pickups=[pickup("material", 1224.0, 768.0)]),
        )
        self.assertIsNone(state.distance_band)


class DeterminismTest(unittest.TestCase):
    def test_same_snapshots_same_states(self):
        snapshots = [
            combat_snapshot(
                wave=index,
                enemies=filler_enemies(6),
                pickups=[pickup("material", 1024.0 + index * 40.0, 768.0)],
            )
            for index in range(1, 6)
        ]
        first = TacticalController()
        second = TacticalController()
        first_states = [
            decide(first, snapshot, now=float(index)) for index, snapshot in enumerate(snapshots)
        ]
        second_states = [
            decide(second, snapshot, now=float(index)) for index, snapshot in enumerate(snapshots)
        ]
        self.assertEqual(first_states, second_states)


if __name__ == "__main__":
    unittest.main()
