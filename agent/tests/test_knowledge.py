"""knowledge.py 测试：仓库知识库加载、版本/哈希告警、Tier 解析（票据 11）。"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ab_agent.knowledge import (
    DATASETS,
    DEFAULT_KNOWLEDGE_DIR,
    KnowledgeError,
    load_knowledge,
)
from ab_agent.protocol import GAME_VERSION

_WRAPPER_KEYS = ("schema_version", "game_version", "data_version", "mod_version")


def _content_hash(payload):
    """测试侧按 strategy.md §7 规范独立实现（与被测模块重算逻辑互不引用）。"""
    body = {key: payload[key] for key in ("schema_version", "game_version", "entries")}
    for key, value in payload.items():
        if key not in _WRAPPER_KEYS and key != "entries":
            body[key] = value
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dataset(entries, game_version=GAME_VERSION, schema_version=1, extra=None):
    payload = {
        "schema_version": schema_version,
        "game_version": game_version,
        "mod_version": "0.2.0",
        "entries": entries,
    }
    if extra:
        payload.update(extra)
    payload["data_version"] = _content_hash(payload)
    return payload


def _write_knowledge(base: Path, *, game_version=GAME_VERSION, tier_list=None, entries=None):
    entries = entries or {}
    for name in DATASETS:
        payload = _dataset(entries.get(name, []), game_version=game_version)
        (base / (name + ".json")).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    if tier_list is not None:
        (base / "tier_list.json").write_text(
            json.dumps(tier_list, ensure_ascii=False), encoding="utf-8"
        )


class RepositoryKnowledgeTest(unittest.TestCase):
    """仓库内 docs/knowledge 应可直接加载（导出产物入仓，票据 11 验收）。"""

    def test_default_directory_loads(self):
        knowledge = load_knowledge()
        self.assertEqual(knowledge.directory, DEFAULT_KNOWLEDGE_DIR)
        self.assertEqual(knowledge.game_version, GAME_VERSION)
        self.assertEqual(knowledge.warnings, ())
        self.assertGreater(len(knowledge.items), 100)
        self.assertGreater(len(knowledge.weapons), 100)
        self.assertGreater(len(knowledge.upgrades), 30)
        self.assertGreater(len(knowledge.characters), 30)
        self.assertGreater(len(knowledge.sets), 5)

    def test_data_versions_and_tier_list(self):
        knowledge = load_knowledge()
        self.assertEqual(set(knowledge.data_versions), set(DATASETS))
        for data_version in knowledge.data_versions.values():
            self.assertEqual(len(data_version), 64)
        self.assertTrue(knowledge.tier_ratings)
        self.assertTrue(knowledge.tier_updated_at)

    def test_ranger_reference_entries(self):
        knowledge = load_knowledge()
        ranger = knowledge.characters["character_ranger"]
        self.assertTrue(ranger["unlocked_by_default"])
        self.assertIn("weapon_smg_1", ranger["starting_weapons"])
        pistol = knowledge.weapons["weapon_pistol_1"]
        self.assertEqual(pistol["class"], "ranged")
        self.assertEqual(pistol["price"], 10)
        self.assertEqual(pistol["chain"], ["weapon_pistol_1", "weapon_pistol_2", "weapon_pistol_3", "weapon_pistol_4"])


class LoaderTest(unittest.TestCase):
    def test_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(KnowledgeError, "不存在"):
                load_knowledge(Path(tmp))

    def test_duplicate_id_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_knowledge(base, entries={"items": [{"id": "a"}, {"id": "a"}]})
            with self.assertRaisesRegex(KnowledgeError, "重复 id"):
                load_knowledge(base)

    def test_wrapper_field_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_knowledge(base)
            path = base / "items.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.pop("data_version")
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(KnowledgeError, "data_version"):
                load_knowledge(base)

    def test_game_version_mismatch_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_knowledge(base, game_version="0.0.0")
            knowledge = load_knowledge(base)
            self.assertEqual(knowledge.game_version, "0.0.0")
            mismatch = [warning for warning in knowledge.warnings if "不匹配" in warning]
            self.assertEqual(len(mismatch), 1)

    def test_expected_game_version_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_knowledge(base, game_version="9.9.9")
            knowledge = load_knowledge(base, expected_game_version=None)
            self.assertFalse(any("不匹配" in warning for warning in knowledge.warnings))

    def test_missing_tier_list_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_knowledge(base)
            knowledge = load_knowledge(base)
            self.assertTrue(any("tier_list" in warning for warning in knowledge.warnings))

    def test_tampered_entries_warn_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_knowledge(base, entries={"items": [{"id": "a"}]})
            path = base / "items.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["entries"].append({"id": "b"})
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            knowledge = load_knowledge(base)
            mismatches = [w for w in knowledge.warnings if "data_version 校验失败" in w]
            self.assertEqual(len(mismatches), 1)
            self.assertIn("items.json", mismatches[0])


class TierListTest(unittest.TestCase):
    def test_exact_and_family_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_knowledge(
                base,
                entries={
                    "items": [{"id": "item_x"}],
                    "weapons": [
                        {"id": "weapon_w_1", "weapon_id": "weapon_w"},
                        {"id": "weapon_w_2", "weapon_id": "weapon_w"},
                    ],
                },
                tier_list={
                    "updated_at": "2026-10-06",
                    "ratings": {
                        "item_x": {"tier": "A", "note": "物品"},
                        "weapon_w": {"tier": "S", "note": "武器族"},
                        "weapon_w_2": {"tier": "B"},
                    },
                },
            )
            knowledge = load_knowledge(base)
            self.assertEqual(knowledge.warnings, ())
            self.assertEqual(knowledge.tier_rating(knowledge.items["item_x"])["tier"], "A")
            # 精确 id 优先于族回退
            self.assertEqual(knowledge.tier_rating(knowledge.weapons["weapon_w_1"])["tier"], "S")
            self.assertEqual(knowledge.tier_rating(knowledge.weapons["weapon_w_2"])["tier"], "B")
            self.assertIsNone(knowledge.tier_rating({"id": "missing"}))

    def test_invalid_grade_and_unknown_id_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_knowledge(
                base,
                entries={"items": [{"id": "item_x"}]},
                tier_list={"ratings": {"item_x": {"tier": "Z"}, "nope": {"tier": "A"}}},
            )
            knowledge = load_knowledge(base)
            self.assertTrue(any("tier='Z'" in warning for warning in knowledge.warnings))
            self.assertTrue(any("nope" in warning for warning in knowledge.warnings))
            # 非法评级直接丢弃（避免脏数据流入决策层）；未知 id 保留但不生效
            self.assertNotIn("item_x", knowledge.tier_ratings)
            self.assertIn("nope", knowledge.tier_ratings)

    def test_tier_list_game_version_mismatch_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_knowledge(
                base,
                entries={"items": [{"id": "item_x"}]},
                tier_list={
                    "game_version": "0.0.0",
                    "ratings": {"item_x": {"tier": "A"}},
                },
            )
            knowledge = load_knowledge(base)
            self.assertTrue(any("不一致" in warning for warning in knowledge.warnings))


if __name__ == "__main__":
    unittest.main()
