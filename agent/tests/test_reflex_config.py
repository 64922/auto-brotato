"""decision/config.py 测试：默认文件、结构校验与错误信息。"""
import json
import tempfile
import unittest
from pathlib import Path

from ab_agent.decision.config import (
    DEFAULT_CONFIG_PATH,
    ReflexConfig,
    ReflexConfigError,
    load_reflex_config,
    reflex_config_from_mapping,
)


def _default_raw() -> dict:
    return json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


class LoadDefaultTest(unittest.TestCase):
    def test_default_file_loads(self):
        config = load_reflex_config()
        self.assertIsInstance(config, ReflexConfig)
        self.assertEqual(len(config.danger.sample_times_s), len(config.danger.sample_decay))
        self.assertLess(config.kiting.default_band[0], config.kiting.default_band[1])
        self.assertTrue(config.invuln.enabled)

    def test_strict_sections(self):
        raw = _default_raw()
        raw.pop("invuln")
        with self.assertRaisesRegex(ReflexConfigError, "invuln"):
            reflex_config_from_mapping(raw)


class ValidationTest(unittest.TestCase):
    def test_unknown_key_rejected(self):
        raw = _default_raw()
        raw["decision"]["unknown_knob"] = 1
        with self.assertRaisesRegex(ReflexConfigError, "未知键"):
            reflex_config_from_mapping(raw)

    def test_missing_key_rejected(self):
        raw = _default_raw()
        raw["danger"].pop("truncated_scale")
        with self.assertRaisesRegex(ReflexConfigError, "truncated_scale"):
            reflex_config_from_mapping(raw)

    def test_sample_lengths_must_match(self):
        raw = _default_raw()
        raw["danger"]["sample_decay"] = [1.0, 0.9]
        with self.assertRaisesRegex(ReflexConfigError, "长度"):
            reflex_config_from_mapping(raw)

    def test_sample_times_increasing(self):
        raw = _default_raw()
        raw["danger"]["sample_times_s"] = [0.3, 0.15, 0.5]
        raw["danger"]["sample_decay"] = [1.0, 0.9, 0.75]
        with self.assertRaisesRegex(ReflexConfigError, "递增"):
            reflex_config_from_mapping(raw)

    def test_magnitude_bounds(self):
        raw = _default_raw()
        raw["decision"]["min_magnitude"] = 0.9
        raw["decision"]["max_magnitude"] = 0.5
        with self.assertRaisesRegex(ReflexConfigError, "min_magnitude"):
            reflex_config_from_mapping(raw)

    def test_candidate_count_bounds(self):
        raw = _default_raw()
        raw["decision"]["candidate_count"] = 4
        with self.assertRaisesRegex(ReflexConfigError, "candidate_count"):
            reflex_config_from_mapping(raw)

    def test_band_order(self):
        raw = _default_raw()
        raw["kiting"]["default_band"] = [400.0, 200.0]
        with self.assertRaisesRegex(ReflexConfigError, "default_band"):
            reflex_config_from_mapping(raw)

    def test_bool_is_not_number(self):
        raw = _default_raw()
        raw["player"]["radius"] = True
        with self.assertRaisesRegex(ReflexConfigError, "radius"):
            reflex_config_from_mapping(raw)

    def test_missing_file(self):
        with self.assertRaisesRegex(ReflexConfigError, "无法读取"):
            load_reflex_config(Path(tempfile.gettempdir()) / "no-such-reflex.json")

    def test_bad_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(ReflexConfigError, "JSON"):
                load_reflex_config(path)

    def test_custom_file_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = _default_raw()
            raw["danger"]["truncated_scale"] = 2.0
            path = Path(tmp) / "custom.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            config = load_reflex_config(path)
            self.assertEqual(config.danger.truncated_scale, 2.0)


if __name__ == "__main__":
    unittest.main()
