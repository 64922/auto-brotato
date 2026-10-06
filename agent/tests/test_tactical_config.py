"""decision/tactical_config.py 测试：默认文件、结构校验与错误信息。"""
import json
import tempfile
import unittest
from pathlib import Path

from ab_agent.decision.tactical_config import (
    DEFAULT_CONFIG_PATH,
    TacticalConfig,
    TacticalConfigError,
    load_tactical_config,
    tactical_config_from_mapping,
)


def _default_raw() -> dict:
    return json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


class LoadDefaultTest(unittest.TestCase):
    def test_default_file_loads(self):
        config = load_tactical_config()
        self.assertIsInstance(config, TacticalConfig)
        self.assertAlmostEqual(config.update.interval_s, 0.25)
        self.assertLess(config.boss.band[0], config.boss.band[1])
        self.assertLess(config.conservative.enter_hp_ratio, config.conservative.exit_hp_ratio)

    def test_strict_sections(self):
        raw = _default_raw()
        raw.pop("boss")
        with self.assertRaisesRegex(TacticalConfigError, "boss"):
            tactical_config_from_mapping(raw)


class ValidationTest(unittest.TestCase):
    def test_unknown_key_rejected(self):
        raw = _default_raw()
        raw["cluster"]["unknown_knob"] = 1
        with self.assertRaisesRegex(TacticalConfigError, "未知键"):
            tactical_config_from_mapping(raw)

    def test_missing_key_rejected(self):
        raw = _default_raw()
        raw["rhythm"].pop("late_wave_min")
        with self.assertRaisesRegex(TacticalConfigError, "late_wave_min"):
            tactical_config_from_mapping(raw)

    def test_hysteresis_order_enforced(self):
        raw = _default_raw()
        raw["conservative"]["enter_hp_ratio"] = 0.8
        raw["conservative"]["exit_hp_ratio"] = 0.6
        with self.assertRaisesRegex(TacticalConfigError, "迟滞"):
            tactical_config_from_mapping(raw)

    def test_wave_boundaries_ordered(self):
        raw = _default_raw()
        raw["rhythm"]["early_wave_max"] = 20
        with self.assertRaisesRegex(TacticalConfigError, "early_wave_max"):
            tactical_config_from_mapping(raw)

    def test_boss_band_order(self):
        raw = _default_raw()
        raw["boss"]["band"] = [600.0, 300.0]
        with self.assertRaisesRegex(TacticalConfigError, "boss.band"):
            tactical_config_from_mapping(raw)

    def test_bool_is_not_number(self):
        raw = _default_raw()
        raw["cluster"]["join_radius"] = True
        with self.assertRaisesRegex(TacticalConfigError, "join_radius"):
            tactical_config_from_mapping(raw)

    def test_interval_bounds(self):
        raw = _default_raw()
        raw["update"]["interval_s"] = 2.0
        with self.assertRaisesRegex(TacticalConfigError, "interval_s"):
            tactical_config_from_mapping(raw)

    def test_missing_file(self):
        with self.assertRaisesRegex(TacticalConfigError, "无法读取"):
            load_tactical_config(Path(tempfile.gettempdir()) / "no-such-tactical.json")

    def test_bad_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(TacticalConfigError, "JSON"):
                load_tactical_config(path)

    def test_custom_file_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = _default_raw()
            raw["consumable"]["hp_ratio"] = 0.8
            path = Path(tmp) / "custom.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            config = load_tactical_config(path)
            self.assertAlmostEqual(config.consumable.hp_ratio, 0.8)


if __name__ == "__main__":
    unittest.main()
