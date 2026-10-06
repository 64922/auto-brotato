"""economy.py：回放经济层报告（提取/去重、重放、确定性、参数对比与 CLI）。"""
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools.replay.economy import (
    _apply_purchase,
    build_configs,
    collect_facts,
    format_report,
    main,
    simulate,
)
from tools.replay.recording import open_recording
from tools.replay.tests.support import in_line, snapshot, write_recording

from ab_agent.decision.economy import EconomyPlanner
from ab_agent.decision.economy_config import DEFAULT_CONFIG_PATH, load_economy_config
from ab_agent.decision.economy_model import Appraisal, ScoreBreakdown
from ab_agent.knowledge import load_knowledge

KNOWLEDGE = load_knowledge()


def shop_payload(*, wave, gold, slots, inventory=None):
    return {
        "wave_next": wave,
        "gold": gold,
        "slots": slots,
        "inventory": inventory or {"weapons": [], "items": []},
        "stats": {"ranged_damage": 0},
        "reroll": {"cost": 3, "count": 0},
        "can_leave": True,
    }


def slot(index, kind, id, price, tier=0):
    return {
        "slot": index,
        "kind": kind,
        "id": id,
        "tier": tier,
        "price": price,
        "sold": False,
        "locked": False,
    }


def recording_records():
    starter = {"weapons": [{"slot": 0, "id": "weapon_pistol", "tier": 0}], "items": []}
    final = {
        "weapons": [
            {"slot": 0, "id": "weapon_pistol", "tier": 0},
            {"slot": 1, "id": "weapon_smg", "tier": 0},
        ],
        "items": [],
    }
    return [
        snapshot(100.0, wave={"index": 1, "phase": "combat"}, inventory=starter),
        in_line(101.0, "shop", shop_payload(wave=2, gold=50, slots=[slot(0, "weapon", "weapon_smg_1", 20)], inventory=starter)),
        in_line(101.5, "shop", shop_payload(wave=2, gold=50, slots=[slot(0, "weapon", "weapon_smg_1", 20)], inventory=starter)),
        in_line(102.0, "shop", shop_payload(wave=3, gold=70, slots=[slot(0, "item", "item_acid", 65)], inventory=starter)),
        snapshot(103.0, wave={"index": 3, "phase": "combat"}, inventory=final, stats={"ranged_damage": 10}),
    ]


class EconomyReplayTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="ab-eco-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.path = write_recording(self.directory / "run.ndjson", recording_records())
        self.facts = collect_facts(open_recording(self.path))

    def planner(self, **shop_overrides):
        raw = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        raw["shop"].update(shop_overrides)
        from ab_agent.decision.economy_config import economy_config_from_mapping

        return EconomyPlanner(KNOWLEDGE, economy_config_from_mapping(raw))

    def test_collect_dedupes_heartbeats_and_final_inventory(self):
        self.assertEqual(len(self.facts.shop_frames), 2)
        self.assertEqual([frame.wave for frame in self.facts.shop_frames], [2, 3])
        self.assertEqual(self.facts.final_inventory["weapons"][1]["id"], "weapon_smg")
        self.assertEqual(self.facts.final_stats, {"ranged_damage": 10})

    def test_simulate_buys_and_gold_curve(self):
        result = simulate(self.facts, self.planner(), "默认配置")
        self.assertEqual(result.buy_count, 1)
        self.assertEqual(result.purchases[0].id, "weapon_smg_1")
        self.assertEqual(result.planned_spend, 20)
        self.assertEqual(result.gold_curve, ((2, 50, 20), (3, 70, 20)))
        self.assertGreater(result.final_strength, 0.0)

    def test_simulate_is_deterministic(self):
        first = simulate(self.facts, self.planner(), "a")
        second = simulate(self.facts, self.planner(), "a")
        self.assertEqual(first, second)

    def test_threshold_changes_purchase_sequence(self):
        strict = simulate(self.facts, self.planner(buy_threshold=100.0), "严格")
        loose = simulate(self.facts, self.planner(), "默认")
        self.assertEqual(strict.buy_count, 0)
        self.assertEqual(loose.buy_count, 1)

    def test_merge_approximation_upgrades_shadow_weapon(self):
        planner = self.planner()
        shadow = {"weapons": [{"slot": 0, "id": "weapon_smg", "tier": 0}], "items": []}
        appraisal = Appraisal(
            slot=0,
            kind="weapon",
            id="weapon_smg_1",
            tier=0,
            price=20,
            score=ScoreBreakdown(),
            known=True,
        )
        merged = _apply_purchase(shadow, appraisal, planner)
        self.assertEqual(len(merged["weapons"]), 1)
        self.assertEqual(merged["weapons"][0]["id"], "weapon_smg_2")
        self.assertEqual(merged["weapons"][0]["tier"], 1)

    def test_item_purchase_merges_count(self):
        planner = self.planner()
        shadow = {"weapons": [], "items": [{"id": "item_acid", "count": 1}]}
        appraisal = Appraisal(
            slot=0,
            kind="item",
            id="item_acid",
            tier=0,
            price=65,
            score=ScoreBreakdown(),
            known=True,
        )
        merged = _apply_purchase(shadow, appraisal, planner)
        self.assertEqual(merged["items"], [{"id": "item_acid", "count": 2}])

    def test_build_configs_default_and_dedup(self):
        choices = build_configs([])
        self.assertEqual(len(choices), 1)
        self.assertIsNone(choices[0].path)
        same = self.directory / "same.json"
        same.write_text(json.dumps(json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))), encoding="utf-8")
        choices = build_configs([str(same), str(same)])
        self.assertEqual(len(choices), 1)

    def test_report_contains_sections(self):
        results = [simulate(self.facts, self.planner(), "默认配置")]
        report = format_report(self.facts, results)
        self.assertIn("回放经济层报告", report)
        self.assertIn("[参数集对比]", report)
        self.assertIn("[购买序列]", report)
        self.assertIn("weapon_smg_1", report)
        self.assertIn("[金币曲线]", report)

    def test_cli_json_and_missing_file(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main([str(self.path), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["shop_views"], 2)
        self.assertEqual(payload["configs"][0]["buy_count"], 1)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main([str(self.directory / "missing.ndjson")])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
