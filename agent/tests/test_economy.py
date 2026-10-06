"""economy.py / economy_scoring.py 测试：评分分量、商店规则与升级选卡（票据 12）。"""
import json
import unittest

from ab_agent.decision.economy import (
    ACTION_BUY,
    ACTION_LEAVE,
    ACTION_LOCK,
    ACTION_PICK,
    ACTION_REROLL,
    ACTION_SELL,
    EconomyPlanner,
)
from ab_agent.decision.economy_config import (
    DEFAULT_CONFIG_PATH,
    economy_config_from_mapping,
)
from ab_agent.decision.economy_model import EconomyContext
from ab_agent.knowledge import load_knowledge

KNOWLEDGE = load_knowledge()
SMG = "weapon_smg_1"
PISTOL = "weapon_pistol_1"


def raw_config() -> dict:
    return json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


def make_planner(**shop_overrides) -> EconomyPlanner:
    raw = raw_config()
    raw["shop"].update(shop_overrides)
    return EconomyPlanner(KNOWLEDGE, economy_config_from_mapping(raw))


def weapon(slot, id, tier=0, price=10, *, sold=False):
    return {
        "slot": slot,
        "kind": "weapon",
        "id": id,
        "tier": tier,
        "price": price,
        "sold": sold,
        "locked": False,
    }


def item(slot, id, price=10, *, sold=False):
    return {
        "slot": slot,
        "kind": "item",
        "id": id,
        "tier": 0,
        "price": price,
        "sold": sold,
        "locked": False,
    }


def inventory(weapons=(), items=()):
    return {
        "weapons": [
            {"slot": index, "id": wid, "tier": tier}
            for index, (wid, tier) in enumerate(weapons)
        ],
        "items": [{"id": iid, "count": count} for iid, count in items],
    }


def context(
    *,
    wave=2,
    gold=50,
    stats=None,
    weapons=(),
    items_=(),
    hero="character_ranger",
) -> EconomyContext:
    return EconomyContext(
        wave=wave,
        gold=gold,
        stats=stats or {},
        inventory=inventory(weapons=weapons, items=items_),
        hero_id=hero,
    )


def make_shop(slots, *, wave=2, gold=50, reroll_cost=3, reroll_count=0):
    return {
        "wave_next": wave,
        "gold": gold,
        "slots": slots,
        "inventory": {"weapons": [], "items": []},
        "stats": {},
        "reroll": {"cost": reroll_cost, "count": reroll_count},
        "can_leave": True,
    }


class ScoreTest(unittest.TestCase):
    def setUp(self):
        self.planner = make_planner()
        self.scorer = self.planner.scorer
        self.profile = self.planner.profile("character_ranger", inventory())

    def score(self, kind, id, *, tier=0, price=10, ctx=None, full=False):
        ctx = ctx or context()
        return self.scorer.score_offer(
            kind,
            id,
            tier,
            price=price,
            context=ctx,
            profile=self.planner.profile(ctx.hero_id, ctx.inventory),
            full_penalty=full,
        )

    def test_smg_scores_above_pistol(self):
        smg = self.score("weapon", SMG, price=20)
        pistol = self.score("weapon", PISTOL, price=10)
        self.assertGreater(smg.total, pistol.total)
        self.assertIn("Tier S", smg.flags)
        self.assertGreater(smg.stat_value, pistol.stat_value)

    def test_merge_bonus_exact(self):
        plain = self.score("weapon", SMG, price=20)
        merged = self.score("weapon", SMG, price=20, ctx=context(weapons=[("weapon_smg", 0)]))
        self.assertIn("合并", merged.flags)
        self.assertNotIn("合并", plain.flags)
        self.assertAlmostEqual(
            merged.total - plain.total, raw_config()["scoring"]["merge_bonus"]
        )

    def test_set_synergy_positive_with_existing_set(self):
        score = self.score(
            "weapon", SMG, price=20, ctx=context(weapons=[("weapon_pistol", 0)])
        )
        self.assertGreater(score.synergy, 0.0)

    def test_price_cost_higher_early(self):
        early = self.score("item", "item_helmet", price=15, ctx=context(wave=2))
        late = self.score("item", "item_helmet", price=15, ctx=context(wave=15))
        self.assertLess(early.total, late.total)
        self.assertLess(early.price, 0.0)

    def test_full_weapon_penalty_only_for_non_merge(self):
        plain = self.scorer.score_offer(
            "weapon", SMG, 0, price=20,
            context=context(
                weapons=[("weapon_pistol", 0), ("weapon_laser_gun", 0), ("weapon_revolver", 0)]
            ),
            profile=self.profile, full_penalty=True,
        )
        self.assertIn("武器位满", plain.flags)
        merged = self.scorer.score_offer(
            "weapon", SMG, 0, price=20,
            context=context(
                weapons=[("weapon_smg", 0), ("weapon_laser_gun", 0), ("weapon_revolver", 0)]
            ),
            profile=self.profile, full_penalty=True,
        )
        self.assertIn("合并", merged.flags)
        self.assertNotIn("武器位满", merged.flags)
        self.assertGreater(merged.total, plain.total)

    def test_unknown_entry_has_no_value(self):
        score = self.score("item", "item_unknown_xyz", price=10)
        self.assertFalse(score.flags == ())
        self.assertLess(score.total, 0.0)
        self.assertIn("未知条目", score.flags)

    def test_missing_knowledge_degrades(self):
        planner = EconomyPlanner(None)
        plan = planner.plan_shop(make_shop([item(0, "item_helmet")]), context())
        self.assertEqual(plan.action, ACTION_LEAVE)
        self.assertIn("降级", plan.reason)


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.planner = make_planner()

    def test_hero_profile_exact_match(self):
        profile = self.planner.profile("character_ranger", inventory())
        self.assertEqual(profile.weights["ranged_damage"], 1.0)

    def test_unknown_hero_falls_back_by_weapon_class(self):
        ranged = self.planner.profile("character_unknown", inventory([("weapon_pistol", 0)]))
        self.assertEqual(ranged.weights["ranged_damage"], 1.0)
        self.assertEqual(ranged.weights["melee_damage"], 0.0)
        melee = self.planner.profile("character_unknown", inventory([("weapon_knife", 0)]))
        self.assertEqual(melee.weights["melee_damage"], 1.0)

    def test_unknown_hero_without_weapons_uses_fallback(self):
        profile = self.planner.profile("character_unknown", inventory())
        self.assertEqual(profile.weights["ranged_damage"], 1.0)


class ShopPlanTest(unittest.TestCase):
    def setUp(self):
        self.planner = make_planner()

    def test_buy_best_when_affordable(self):
        shop = make_shop([weapon(0, SMG, price=20), item(1, "item_acid", 65)], gold=50)
        plan = self.planner.plan_shop(shop, context(gold=50))
        self.assertEqual(plan.action, ACTION_BUY)
        self.assertEqual(plan.params, {"slot": 0})
        self.assertEqual(plan.target_key, "shop_buy:0")
        self.assertIn("购买", plan.reason)

    def test_leave_when_nothing_valuable(self):
        shop = make_shop([item(0, "item_acid", 65)], reroll_cost=-1)
        plan = self.planner.plan_shop(shop, context(gold=100))
        self.assertEqual(plan.action, ACTION_LEAVE)

    def test_safety_margin_blocks_expensive_buy(self):
        shop = make_shop([weapon(0, SMG, price=20)], gold=30)
        plan = self.planner.plan_shop(shop, context(gold=30))
        self.assertEqual(plan.action, ACTION_LEAVE, plan.reason)

    def test_lock_valuable_but_unaffordable(self):
        shop = make_shop([weapon(0, SMG, price=30)], gold=10)
        plan = self.planner.plan_shop(shop, context(gold=10))
        self.assertEqual(plan.action, ACTION_LOCK)
        self.assertEqual(plan.params, {"slot": 0, "locked": True})
        self.assertEqual(plan.target_key, "shop_lock:0")

    def test_reroll_when_all_low_within_budget(self):
        shop = make_shop([item(0, "item_acid", 65)], gold=30)
        plan = self.planner.plan_shop(shop, context(gold=30))
        self.assertEqual(plan.action, ACTION_REROLL)
        plan = self.planner.plan_shop(shop, context(gold=30), rerolls_used=2)
        self.assertEqual(plan.action, ACTION_LEAVE)

    def test_blocked_buy_falls_back_to_lock(self):
        shop = make_shop([weapon(0, SMG, price=30)], gold=10)
        plan = self.planner.plan_shop(
            shop, context(gold=10), blocked={"shop_buy:0"}
        )
        self.assertEqual(plan.action, ACTION_LOCK)

    def test_sell_low_value_weapon_when_slots_full(self):
        six = [(PISTOL, 0)] * 6
        shop = make_shop([weapon(0, SMG, price=20)], gold=100)
        plan = self.planner.plan_shop(shop, context(gold=100, weapons=six))
        self.assertEqual(plan.action, ACTION_SELL)
        self.assertEqual(plan.params, {"inv_kind": "weapon", "index": 0})
        self.assertIn("武器位已满", plan.reason)

    def test_merge_buy_does_not_sell(self):
        inv = [(PISTOL, 0)] * 5 + [("weapon_smg", 0)]
        shop = make_shop([weapon(0, SMG, price=20)], gold=100)
        plan = self.planner.plan_shop(shop, context(gold=100, weapons=inv))
        self.assertEqual(plan.action, ACTION_BUY)

    def test_no_sell_when_weapons_all_valuable(self):
        inv = [("weapon_laser_gun", 0)] * 6
        shop = make_shop([weapon(0, "weapon_minigun_3", tier=2, price=20)], gold=100)
        plan = self.planner.plan_shop(shop, context(gold=100, weapons=inv))
        self.assertEqual(plan.action, ACTION_LEAVE, plan.reason)

    def test_sell_for_gold_when_short(self):
        shop = make_shop([weapon(0, SMG, price=60)], gold=20)
        plan = self.planner.plan_shop(
            shop, context(gold=20, weapons=[(PISTOL, 0), (PISTOL, 0)])
        )
        self.assertEqual(plan.action, ACTION_SELL)
        self.assertIn("金币不足", plan.reason)

    def test_no_sell_below_min_weapons(self):
        shop = make_shop([weapon(0, SMG, price=40)], gold=20)
        plan = self.planner.plan_shop(shop, context(gold=20, weapons=[(PISTOL, 0)]))
        self.assertEqual(plan.action, ACTION_LOCK)

    def test_plan_is_deterministic(self):
        shop = make_shop([weapon(0, SMG, price=20), item(1, "item_acid", 65)], gold=50)
        ctx = context(gold=50)
        self.assertEqual(
            self.planner.plan_shop(shop, ctx),
            self.planner.plan_shop(shop, ctx),
        )


class LevelUpPlanTest(unittest.TestCase):
    def setUp(self):
        self.planner = make_planner()

    def menu(self, options):
        return {
            "phase": "level_up",
            "wave": 2,
            "options": [
                {
                    "slot": slot,
                    "kind": kind,
                    "id": id,
                    "tier": 0,
                    "can_pick": can_pick,
                }
                for slot, kind, id, can_pick in options
            ],
        }

    def test_picks_highest_scoring_upgrade(self):
        menu = self.menu(
            [
                (1, "upgrade", "upgrade_engineering_1", True),
                (2, "upgrade", "upgrade_ranged_damage_1", True),
            ]
        )
        plan = self.planner.plan_level_up(menu, context())
        self.assertEqual(plan.slot, 2)
        self.assertEqual(plan.target_key, "menu_pick_upgrade:2")

    def test_need_fills_weakest_stat(self):
        menu = self.menu(
            [
                (1, "upgrade", "upgrade_ranged_damage_1", True),
                (2, "upgrade", "upgrade_crit_chance_1", True),
            ]
        )
        satisfied = context(stats={"ranged_damage": 50.0})
        plan = self.planner.plan_level_up(menu, satisfied)
        self.assertEqual(plan.slot, 2)
        self.assertIn("最缺", plan.reason)

    def test_unknown_card_never_beats_known(self):
        menu = self.menu(
            [
                (1, "upgrade", "upgrade_unknown_xyz", True),
                (2, "upgrade", "upgrade_ranged_damage_1", True),
            ]
        )
        plan = self.planner.plan_level_up(menu, context())
        self.assertEqual(plan.slot, 2)

    def test_unknown_card_uses_config_penalty(self):
        menu = self.menu(
            [
                (1, "upgrade", "upgrade_unknown_xyz", True),
                (2, "upgrade", "upgrade_ranged_damage_1", True),
            ]
        )
        plan = self.planner.plan_level_up(menu, context())
        unknown = next(a for a in plan.appraisals if a.id == "upgrade_unknown_xyz")
        self.assertEqual(
            unknown.score.total, self.planner.config.upgrade.unknown_score
        )

    def test_no_pickable_returns_none(self):
        menu = self.menu([(1, "upgrade", "upgrade_ranged_damage_1", False)])
        plan = self.planner.plan_level_up(menu, context())
        self.assertIsNone(plan.slot)

    def test_weapon_card_full_penalty(self):
        six = [(PISTOL, 0)] * 6
        menu = self.menu([(1, "weapon", SMG, True)])
        ctx = context(weapons=six)
        full_plan = self.planner.plan_level_up(menu, ctx)
        no_penalty = self.planner.scorer.score_offer(
            "weapon",
            SMG,
            0,
            price=0,
            context=ctx,
            profile=self.planner.profile(ctx.hero_id, ctx.inventory),
            full_penalty=False,
            use_need=True,
        )
        self.assertIn("武器位满", full_plan.appraisals[0].score.flags)
        self.assertAlmostEqual(
            no_penalty.total - full_plan.appraisals[0].score.total,
            raw_config()["scoring"]["weapon_full_penalty"],
        )

    def test_missing_knowledge_picks_first(self):
        planner = EconomyPlanner(None)
        menu = self.menu(
            [
                (1, "upgrade", "upgrade_engineering_1", True),
                (2, "upgrade", "upgrade_ranged_damage_1", True),
            ]
        )
        plan = planner.plan_level_up(menu, context())
        self.assertEqual(plan.slot, 1)
        self.assertIn("降级", plan.reason)

    def test_same_options_same_plan(self):
        menu = self.menu(
            [
                (1, "upgrade", "upgrade_engineering_1", True),
                (2, "upgrade", "upgrade_attack_speed_1", True),
            ]
        )
        ctx = context()
        self.assertEqual(
            self.planner.plan_level_up(menu, ctx),
            self.planner.plan_level_up(menu, ctx),
        )


class StrengthTest(unittest.TestCase):
    def setUp(self):
        self.planner = make_planner()

    def strength(self, weapons=(), items_=()):
        return self.planner.inventory_strength(
            inventory(weapons=weapons, items=items_), context()
        )

    def test_stronger_weapon_scores_higher(self):
        self.assertGreater(self.strength(weapons=[(SMG, 0)]), self.strength(weapons=[(PISTOL, 0)]))

    def test_higher_tier_scores_higher(self):
        self.assertGreater(
            self.strength(weapons=[("weapon_smg", 1)]),
            self.strength(weapons=[("weapon_smg", 0)]),
        )

    def test_items_contribute(self):
        self.assertGreater(self.strength(items_=[("item_helmet", 1)]), 0.0)


if __name__ == "__main__":
    unittest.main()
