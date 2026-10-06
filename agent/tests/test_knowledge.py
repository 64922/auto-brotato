"""knowledge.py 测试：仓库知识库加载、版本告警、Tier 解析（票据 11）。"""
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


def _dataset(entries, game_version=GAME_VERSION, schema_version=1, extra=None):
    payload = {
        "schema_version": schema_version,
        "game_version": game_version,
        "data_version": "0" * 64,
        "mod_version": "0.2.0",
        "entries": entries,
    }
    if extra:
        payload.update(extra)
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
            # 非法评级条目仍保留（评分侧自行兜底），不抛异常
            self.assertIn("item_x", knowledge.tier_ratings)

    def test_character_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_knowledge(
                base,
                entries={
                    "characters": [
                        {"id": "character_ranger", "name": "游侠"},
                        {"id": "character_x"},
                    ]
                },
            )
            knowledge = load_knowledge(base)
            names = knowledge.character_names()
            self.assertEqual(names["character_ranger"], "游侠")
            self.assertEqual(names["character_x"], "character_x")


if __name__ == "__main__":
    unittest.main()
