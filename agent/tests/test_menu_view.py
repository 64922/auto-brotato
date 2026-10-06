"""menu_view.py 单元测试：难度页/终局解析与终端文案。"""
import unittest

from ab_agent.menu_view import (
    BattleReport,
    DifficultyMenu,
    RunEndMenu,
    first_pickable_slot,
    format_duration,
    format_level_up_summary,
    format_shop_summary,
)

from tests import payloads


class DifficultyMenuTest(unittest.TestCase):
    def test_parse_real_fields(self):
        menu = DifficultyMenu.from_payload(payloads.difficulty())
        self.assertEqual(menu.hero_id, "character_ranger")
        self.assertEqual(menu.unlocked_values, (0, 1))
        self.assertEqual(menu.max_unlocked, 1)
        self.assertTrue(menu.is_unlocked(1))
        self.assertFalse(menu.is_unlocked(2))
        self.assertEqual(menu.selected_value(), 0)
        self.assertEqual(menu.unlocked_range_text(), "D0–D1")
        self.assertFalse(menu.modes_on())
        self.assertTrue(menu.can_start)

    def test_prompt_contains_required_fields(self):
        text = DifficultyMenu.from_payload(payloads.difficulty()).format_prompt()
        self.assertIn("character_ranger（游侠）", text)
        self.assertIn("weapon_pistol_1", text)
        self.assertIn("D0–D1", text)
        self.assertIn("无尽=关", text)
        self.assertIn("当前选择：D0", text)

    def test_modes_and_confirm_prompt(self):
        menu = DifficultyMenu.from_payload(
            payloads.difficulty(modes={"endless": True, "ban": False, "coop": False})
        )
        self.assertTrue(menu.modes_on())
        text = menu.format_confirm_prompt(1)
        self.assertIn("无尽模式", text)
        self.assertIn("D1", text)

    def test_missing_fields_degrade_safely(self):
        menu = DifficultyMenu.from_payload({})
        self.assertEqual(menu.hero_id, "")
        self.assertEqual(menu.unlocked_values, ())
        self.assertIn("未知", menu.format_prompt())
        self.assertFalse(menu.modes_on())

    def test_non_contiguous_unlocked_range(self):
        menu = DifficultyMenu.from_payload(payloads.difficulty(unlocked=(0, 2)))
        self.assertEqual(menu.unlocked_range_text(), "D0、D2")

    def test_knowledge_names_override(self):
        menu = DifficultyMenu.from_payload(payloads.difficulty())
        text = menu.format_prompt({"character_ranger": "游侠（知识库）"})
        self.assertIn("character_ranger（游侠（知识库））", text)

    def test_knowledge_names_missing_falls_back(self):
        menu = DifficultyMenu.from_payload(payloads.difficulty())
        text = menu.format_prompt({"character_ranger": "character_ranger"})
        self.assertIn("character_ranger（游侠）", text)


class RunEndMenuTest(unittest.TestCase):
    def test_parse(self):
        menu = RunEndMenu.from_payload(payloads.run_end(result="victory", wave=20))
        self.assertEqual(menu.result, "victory")
        self.assertEqual(menu.wave, 20)
        self.assertEqual(menu.result_text(), "胜利")

    def test_unknown_result(self):
        menu = RunEndMenu.from_payload({"result": "unknown", "wave": None})
        self.assertIsNone(menu.result)
        self.assertEqual(menu.result_text(), "未知")


class BattleReportTest(unittest.TestCase):
    def test_report_contains_required_items(self):
        report = BattleReport(
            result="victory",
            wave=20,
            duration_s=754.0,
            gold=321,
            materials=15,
            weapons=({"id": "weapon_pistol", "tier": 2},),
            items=({"id": "item_helmet", "count": 2},),
            stats={"STAT_MAX_HP": "40"},
            replay_path=r"C:\rec\a.ndjson",
        ).format()
        self.assertIn("胜利", report)
        self.assertIn("第 20 波", report)
        self.assertIn("12 分 34 秒", report)
        self.assertIn("金币：321", report)
        self.assertIn("weapon_pistol(T2)", report)
        self.assertIn("item_helmet×2", report)
        self.assertIn("最大生命=40", report)
        self.assertIn(r"C:\rec\a.ndjson", report)

    def test_report_unknown_fields(self):
        report = BattleReport(
            result=None, wave=None, duration_s=None, gold=None, materials=None
        ).format()
        self.assertIn("未知", report)
        self.assertIn("未录制", report)


class HelpersTest(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(format_duration(59), "59 秒")
        self.assertEqual(format_duration(60), "1 分 0 秒")
        self.assertEqual(format_duration(None), "未知")

    def test_first_pickable_slot(self):
        self.assertEqual(first_pickable_slot(payloads.level_up()), 1)
        no_pick = payloads.level_up(picks=(False, False, False, False))
        self.assertIsNone(first_pickable_slot(no_pick))

    def test_shop_and_level_up_summaries(self):
        self.assertIn("离开", format_shop_summary(payloads.shop()))
        summary = format_level_up_summary(payloads.level_up())
        self.assertIn("第 1 波", summary)
        self.assertIn("upgrade_hp_regeneration_1", summary)


if __name__ == "__main__":
    unittest.main()
