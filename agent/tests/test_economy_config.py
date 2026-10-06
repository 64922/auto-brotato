"""economy_config.py 测试：默认配置可加载、键/类型/数值校验与英雄档案引用校验。"""
import json
import unittest

from ab_agent.decision.economy_config import (
    DEFAULT_CONFIG_PATH,
    EconomyConfigError,
    economy_config_from_mapping,
    load_economy_config,
)
from ab_agent.knowledge import load_knowledge


def raw_config() -> dict:
    return json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


class DefaultConfigTest(unittest.TestCase):
    def test_default_config_loads(self):
        config = load_economy_config()
        self.assertGreater(config.shop.buy_threshold, 0.0)
        self.assertEqual(set(config.scoring.tier_bonus), {"S", "A", "B", "C", "D"})
        self.assertIn("character_ranger", config.heroes)
        self.assertEqual(config.hero_assign.heroes["character_ranger"], "character_ranger")
        self.assertEqual(config.hero_assign.by_class["ranged"], "default_ranged")
        self.assertTrue(config.shop.lock_enabled)

    def test_effect_weights_reference_real_effect_keys(self):
        """配置里的效果键都应是知识库真实出现过的 effect 键（防止拼写漂移）。"""
        knowledge = load_knowledge()
        keys = set()
        for dataset in ("items", "weapons", "upgrades"):
            for entry in getattr(knowledge, dataset).values():
                for key in entry.get("stat_deltas") or {}:
                    if not key.startswith("stat_"):
                        keys.add(key)
        for set_entry in knowledge.sets.values():
            for bonus in set_entry.get("bonuses") or []:
                for effect in bonus:
                    key = effect.get("key")
                    if isinstance(key, str) and not key.startswith("stat_"):
                        keys.add(key)
        config = load_economy_config()
        unknown = set(config.scoring.effect_weights) - keys
        self.assertEqual(unknown, set(), "配置引用了不存在的效果键：%s" % sorted(unknown))


class ConfigValidationTest(unittest.TestCase):
    def build(self, **changes):
        raw = raw_config()
        for key, value in changes.items():
            if value is None:
                raw.pop(key, None)
            else:
                raw[key] = value
        return economy_config_from_mapping(raw)

    def test_missing_top_level_section(self):
        with self.assertRaisesRegex(EconomyConfigError, "缺少键.*scoring"):
            self.build(scoring=None)

    def test_unknown_key_rejected(self):
        raw = raw_config()
        raw["shop"]["unknown_key"] = 1
        with self.assertRaisesRegex(EconomyConfigError, "未知键"):
            economy_config_from_mapping(raw)

    def test_missing_tier_grade(self):
        raw = raw_config()
        del raw["scoring"]["tier_bonus"]["D"]
        with self.assertRaisesRegex(EconomyConfigError, "tier_bonus 缺少键"):
            economy_config_from_mapping(raw)

    def test_effect_weight_must_be_number(self):
        raw = raw_config()
        raw["scoring"]["effect_weights"]["knockback"] = "高"
        with self.assertRaisesRegex(EconomyConfigError, "必须是数字"):
            economy_config_from_mapping(raw)

    def test_wave_window_order(self):
        with self.assertRaisesRegex(EconomyConfigError, "early_wave_max"):
            self.build(shop={**raw_config()["shop"], "early_wave_max": 20})

    def test_sell_min_weapons_bound(self):
        with self.assertRaisesRegex(EconomyConfigError, "sell_min_weapons"):
            self.build(
                shop={**raw_config()["shop"], "sell_min_weapons": 7, "max_weapon_slots": 6}
            )

    def test_unknown_profile_reference(self):
        raw = raw_config()
        raw["hero_assign"]["heroes"]["character_ranger"] = "missing_profile"
        with self.assertRaisesRegex(EconomyConfigError, "未定义的英雄档案"):
            economy_config_from_mapping(raw)

    def test_hero_profile_requires_weights_and_targets(self):
        raw = raw_config()
        del raw["heroes"]["character_ranger"]["weights"]
        with self.assertRaisesRegex(EconomyConfigError, "weights"):
            economy_config_from_mapping(raw)

    def test_by_class_requires_both_classes(self):
        raw = raw_config()
        del raw["hero_assign"]["by_class"]["melee"]
        with self.assertRaisesRegex(EconomyConfigError, "by_class 缺少键"):
            economy_config_from_mapping(raw)


if __name__ == "__main__":
    unittest.main()
