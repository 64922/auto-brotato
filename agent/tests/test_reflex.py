"""reflex.py 测试：危险场几何、方向选择、风筝/边界/紧急策略、平滑与节流。"""
import json
import math
import unittest

from ab_agent.decision.config import (
    DEFAULT_CONFIG_PATH,
    load_reflex_config,
    reflex_config_from_mapping,
)
from ab_agent.decision.danger import (
    Threat,
    point_danger,
    select_threats,
    threats_from_snapshot,
)
from ab_agent.decision.reflex import (
    ReflexController,
    TacticalIntent,
    _actuator_sector,
)

ARENA = {"min": [0.0, 0.0], "max": [2048.0, 1536.0]}
CENTER = (1024.0, 768.0)


def make_config(**sections):
    """默认配置 + 分区覆盖（测试专用；生产配置来自 JSON 文件）。"""
    raw = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    for section, values in sections.items():
        raw[section].update(values)
    return reflex_config_from_mapping(raw)


def combat_snapshot(
    *,
    pos=CENTER,
    hp=100.0,
    max_hp=100.0,
    invuln=False,
    alive=True,
    enemies=(),
    projectiles=(),
    hazards=(),
    pickups=(),
    truncated=False,
):
    return {
        "t": 0.0,
        "wave": {"index": 1, "phase": "combat", "time_left": 10.0, "time_total": 20.0},
        "player": {
            "pos": list(pos),
            "vel": [0.0, 0.0],
            "hp": hp,
            "max_hp": max_hp,
            "alive": alive,
            "invuln": invuln,
        },
        "weapons": [],
        "enemies": [dict(enemy) for enemy in enemies],
        "projectiles": [dict(item) for item in projectiles],
        "pickups": [dict(item) for item in pickups],
        "hazards": [dict(item) for item in hazards],
        "arena": ARENA,
        "inventory": {"weapons": [], "items": []},
        "economy": {"gold": 0, "materials_this_wave": 0},
        "truncated": truncated,
    }


def enemy(x, y, *, vel=(0.0, 0.0), radius=20.0):
    return {
        "id": 1,
        "type": "chaser",
        "pos": [x, y],
        "vel": list(vel),
        "hp_pct": 1.0,
        "radius": radius,
        "elite": False,
        "boss": False,
    }


def projectile(x, y, *, vel=(0.0, 0.0), radius=16.0, ttl=None, friendly=False):
    item = {
        "id": 1,
        "pos": [x, y],
        "vel": list(vel),
        "radius": radius,
        "friendly": friendly,
    }
    if ttl is not None:
        item["ttl"] = ttl
    return item


def pickup(kind, x, y):
    return {"kind": kind, "pos": [x, y]}


class ThrottleTest(unittest.TestCase):
    def test_throttles_by_interval(self):
        controller = ReflexController()
        snapshot = combat_snapshot()
        first = controller.next_move(snapshot, now=1.0)
        self.assertIsNotNone(first)
        self.assertIsNone(controller.next_move(snapshot, now=1.01))
        self.assertIsNotNone(controller.next_move(snapshot, now=1.1))

    def test_reset_clears_throttle(self):
        controller = ReflexController()
        snapshot = combat_snapshot()
        self.assertIsNotNone(controller.next_move(snapshot, now=1.0))
        self.assertIsNone(controller.next_move(snapshot, now=1.01))
        controller.reset()
        self.assertIsNotNone(controller.next_move(snapshot, now=1.02))

    def test_skips_without_player(self):
        controller = ReflexController()
        snapshot = combat_snapshot()
        snapshot["player"] = None
        self.assertIsNone(controller.next_move(snapshot, now=1.0))

    def test_skips_when_dead(self):
        controller = ReflexController()
        self.assertIsNone(controller.next_move(combat_snapshot(alive=False), now=1.0))


class DirectionChoiceTest(unittest.TestCase):
    def test_avoids_enemy_ahead(self):
        controller = ReflexController()
        snapshot = combat_snapshot(enemies=[enemy(1224.0, 768.0)])
        vector = controller.next_move(snapshot, now=1.0)
        self.assertLess(vector[0], 0.0)

    def test_avoids_landmine(self):
        controller = ReflexController()
        snapshot = combat_snapshot(hazards=[{"kind": "landmine", "pos": [1224.0, 768.0], "radius": 36.0}])
        vector = controller.next_move(snapshot, now=1.0)
        self.assertLess(vector[0], 0.0)

    def test_boundary_repels_from_edge(self):
        controller = ReflexController()
        snapshot = combat_snapshot(pos=(2018.0, 768.0))
        vector = controller.next_move(snapshot, now=1.0)
        self.assertLess(vector[0], 0.0)

    def test_kiting_pulls_far_enemy_into_band(self):
        controller = ReflexController()
        snapshot = combat_snapshot(enemies=[enemy(1824.0, 768.0)])
        vector = controller.next_move(snapshot, now=1.0)
        self.assertGreater(vector[0], 0.0)

    def test_default_target_is_nearest_material(self):
        controller = ReflexController()
        snapshot = combat_snapshot(pickups=[pickup("material", 1324.0, 768.0)])
        vector = controller.next_move(snapshot, now=1.0)
        self.assertGreater(vector[0], 0.0)

    def test_tactical_expected_position_wins(self):
        controller = ReflexController()
        snapshot = combat_snapshot(pickups=[pickup("material", 1324.0, 768.0)])
        intent = TacticalIntent(expected_position=(924.0, 768.0))
        vector = controller.next_move(snapshot, now=1.0, intent=intent)
        self.assertLess(vector[0], 0.0)

    def test_tactical_distance_band_override(self):
        controller = ReflexController()
        snapshot = combat_snapshot(enemies=[enemy(1224.0, 768.0)])
        far_band = TacticalIntent(distance_band=(600.0, 800.0))
        near_band = TacticalIntent(distance_band=(100.0, 150.0))
        far_vector = controller.next_move(snapshot, now=1.0, intent=far_band)
        controller.reset()
        near_vector = controller.next_move(snapshot, now=1.0, intent=near_band)
        self.assertLess(far_vector[0], near_vector[0])


class EmergencyTest(unittest.TestCase):
    def test_emergency_prefers_consumable_over_material(self):
        controller = ReflexController()
        snapshot = combat_snapshot(
            hp=20.0,
            pickups=[pickup("consumable", 1224.0, 768.0), pickup("material", 924.0, 768.0)],
        )
        vector = controller.next_move(snapshot, now=1.0)
        self.assertGreater(vector[0], 0.0)

    def test_emergency_raises_danger_scale(self):
        controller = ReflexController()
        normal = combat_snapshot(enemies=[enemy(1224.0, 768.0)])
        wounded = combat_snapshot(hp=15.0, enemies=[enemy(1224.0, 768.0)])
        normal_context = controller._build_context(
            normal, normal["player"], CENTER, None
        )
        wounded_context = controller._build_context(
            wounded, wounded["player"], CENTER, None
        )
        self.assertGreater(
            wounded_context.danger_scales["enemy"],
            normal_context.danger_scales["enemy"],
        )


class DangerFieldTest(unittest.TestCase):
    def test_projectile_future_position(self):
        config = load_reflex_config()
        threats = (Threat("projectile", (100.0, 0.0), (600.0, 0.0), 16.0, None),)
        scales = {"projectile": 1.0}
        now_cost = point_danger(threats, (400.0, 0.0), 0.0, config, scales)
        later_cost = point_danger(threats, (400.0, 0.0), 0.5, config, scales)
        self.assertGreater(now_cost, 0.0)
        self.assertGreater(later_cost, now_cost * 10.0)

    def test_ttl_truncates_sampling(self):
        config = load_reflex_config()
        threats = (Threat("projectile", (100.0, 0.0), (600.0, 0.0), 16.0, 0.3),)
        scales = {"projectile": 1.0}
        self.assertGreater(point_danger(threats, (400.0, 0.0), 0.2, config, scales), 0.0)
        self.assertEqual(point_danger(threats, (400.0, 0.0), 0.5, config, scales), 0.0)

    def test_ttl_zero_means_unbounded(self):
        config = load_reflex_config()
        snapshot = combat_snapshot(projectiles=[projectile(100.0, 0.0, vel=(600.0, 0.0), ttl=0)])
        threats = threats_from_snapshot(snapshot, config)
        self.assertEqual(len(threats), 1)
        self.assertIsNone(threats[0].ttl)

    def test_friendly_projectiles_ignored(self):
        config = load_reflex_config()
        snapshot = combat_snapshot(projectiles=[projectile(100.0, 0.0, friendly=True)])
        self.assertEqual(threats_from_snapshot(snapshot, config), ())

    def test_truncated_scales_danger(self):
        controller = ReflexController()
        point = (1500.0, 768.0)
        normal = combat_snapshot(enemies=[enemy(1200.0, 768.0)])
        truncated = combat_snapshot(enemies=[enemy(1200.0, 768.0)], truncated=True)
        self.assertGreater(
            controller.danger_at(truncated, point),
            controller.danger_at(normal, point),
        )

    def test_invuln_crossing_only_when_trapped(self):
        controller = ReflexController()
        trapped = combat_snapshot(invuln=True, enemies=[enemy(1124.0, 768.0)])
        free = combat_snapshot(invuln=True, enemies=[enemy(1724.0, 768.0)])
        plain = combat_snapshot(enemies=[enemy(1124.0, 768.0)])
        trapped_scales = controller._build_context(
            trapped, trapped["player"], CENTER, None
        ).danger_scales
        free_scales = controller._build_context(free, free["player"], CENTER, None).danger_scales
        plain_scales = controller._build_context(
            plain, plain["player"], CENTER, None
        ).danger_scales
        self.assertLess(trapped_scales["projectile"], plain_scales["projectile"])
        self.assertEqual(free_scales["projectile"], plain_scales["projectile"])

    def test_invuln_flag_respects_switch(self):
        controller = ReflexController(make_config(invuln={"enabled": False}))
        trapped = combat_snapshot(invuln=True, enemies=[enemy(1124.0, 768.0)])
        scales = controller._build_context(
            trapped, trapped["player"], CENTER, None
        ).danger_scales
        self.assertEqual(scales["projectile"], 1.0)


class ThreatSelectionTest(unittest.TestCase):
    def test_filters_radius_and_caps_per_kind(self):
        config = make_config(danger={"consider_radius": 100.0, "max_threats_per_kind": 3})
        threats = tuple(
            Threat("enemy", (float(distance), 0.0), (0.0, 0.0), 20.0, None)
            for distance in range(10, 1000, 10)
        ) + (Threat("projectile", (600.0, 0.0), (0.0, 0.0), 16.0, None),)
        selected = select_threats(threats, (0.0, 0.0), config)
        self.assertEqual(len(selected), 3)
        self.assertTrue(all(threat.kind == "enemy" for threat in selected))
        self.assertEqual([threat.pos[0] for threat in selected], [10.0, 20.0, 30.0])

    def test_danger_at_ignores_distant_threats(self):
        controller = ReflexController(
            make_config(danger={"consider_radius": 100.0, "max_threats_per_kind": 80})
        )
        snapshot = combat_snapshot(enemies=[enemy(1500.0, 768.0)])
        self.assertEqual(controller.danger_at(snapshot, CENTER), 0.0)


class ActuatorSectorTest(unittest.TestCase):
    def test_sector_matches_digital_mapping(self):
        self.assertEqual(_actuator_sector((1.0, 0.0)), 1)  # 右
        self.assertEqual(_actuator_sector((-1.0, 0.0)), 2)  # 左
        self.assertEqual(_actuator_sector((0.707, 0.707)), 5)  # 右下
        self.assertEqual(_actuator_sector((-0.707, -0.707)), 10)  # 左上
        self.assertEqual(_actuator_sector((0.01, -0.01)), 0)  # 死区内按无分量

    def test_switch_required_for_meaningful_change(self):
        controller = ReflexController()
        right = combat_snapshot(pickups=[pickup("material", 1524.0, 768.0)])
        first = controller.next_move(right, now=1.0)
        self.assertEqual(_actuator_sector(first), 1)
        blocked = combat_snapshot(
            pickups=[pickup("material", 1524.0, 768.0)],
            hazards=[{"kind": "landmine", "pos": [1150.0, 768.0], "radius": 36.0}],
        )
        after = controller.next_move(blocked, now=1.1)
        self.assertNotEqual(_actuator_sector(after), 1)

    def test_tiny_perturbation_does_not_switch(self):
        controller = ReflexController()
        snapshot = combat_snapshot(pickups=[pickup("material", 1324.0, 768.0)])
        sector = _actuator_sector(controller.next_move(snapshot, now=1.0))
        nudged = combat_snapshot(pickups=[pickup("material", 1324.0, 770.0)])
        for step in range(10):
            vector = controller.next_move(nudged, now=1.05 + step * 0.05)
            self.assertEqual(_actuator_sector(vector), sector)


class SmoothingTest(unittest.TestCase):
    def test_magnitude_clamped(self):
        controller = ReflexController()
        snapshot = combat_snapshot(enemies=[enemy(1224.0, 768.0)])
        for step in range(10):
            vector = controller.next_move(snapshot, now=1.0 + step * 0.1)
            length = math.hypot(vector[0], vector[1])
            self.assertGreaterEqual(length, 0.55 - 1e-3)
            self.assertLessEqual(length, 1.0 + 1e-3)

    def test_direction_is_stable_no_flapping(self):
        controller = ReflexController()
        snapshot = combat_snapshot(
            enemies=[enemy(1274.0, 768.0)],
            pickups=[pickup("material", 724.0, 768.0)],
        )
        vectors = [
            controller.next_move(snapshot, now=1.0 + step * 0.05) for step in range(120)
        ]
        sectors = [_sector(vector) for vector in vectors]
        flips = sum(1 for a, b in zip(sectors, sectors[1:]) if a != b)
        self.assertLessEqual(flips, 2)
        self.assertEqual(len(set(sectors[3:])), 1)


def _sector(vector):
    angle = math.atan2(vector[1], vector[0])
    return int(round((angle % (2.0 * math.pi)) / (math.pi / 4.0))) % 8


if __name__ == "__main__":
    unittest.main()
