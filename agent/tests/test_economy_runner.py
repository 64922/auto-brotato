"""economy_runner.py 测试：串行下发、ack 重试规则、决策落盘与访问边界。"""
import asyncio
import unittest

from ab_agent.decision.economy import EconomyPlanner
from ab_agent.decision.economy_model import EconomyContext
from ab_agent.decision.economy_runner import EconomyRunner
from ab_agent.knowledge import load_knowledge

KNOWLEDGE = load_knowledge()
SMG = "weapon_smg_1"


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeRecorder:
    def __init__(self):
        self.decisions = []

    def record_decision(self, layer, action, reason):
        self.decisions.append((layer, action, reason))


class FakeServer:
    def __init__(self):
        self.actions = []
        self.acks = {}
        self.recorder = FakeRecorder()

    async def send_action(self, kind, params=None, *, track_ack=False):
        ref = len(self.actions) + 1
        self.actions.append((kind, dict(params or {}), ref))
        return ref

    async def wait_ack(self, ref, timeout=None):
        await asyncio.sleep(0)
        return self.acks.get(ref, {"ok": True})


def shop_payload(*, wave=2, gold=50, price=20, slot_id=SMG, reroll_cost=3):
    return {
        "wave_next": wave,
        "gold": gold,
        "slots": [
            {
                "slot": 0,
                "kind": "weapon",
                "id": slot_id,
                "tier": 0,
                "price": price,
                "sold": False,
                "locked": False,
            }
        ],
        "inventory": {"weapons": [], "items": []},
        "stats": {},
        "reroll": {"cost": reroll_cost, "count": 0},
        "can_leave": True,
    }


def context(**overrides):
    values = {"wave": 2, "gold": 50, "stats": {}, "inventory": {"weapons": [], "items": []}}
    values.update(overrides)
    return EconomyContext(
        wave=values["wave"],
        gold=values["gold"],
        stats=values["stats"],
        inventory=values["inventory"],
        hero_id="character_ranger",
    )


def level_up_menu(*, ids=("upgrade_ranged_damage_1", "upgrade_engineering_1")):
    return {
        "phase": "level_up",
        "wave": 2,
        "options": [
            {"slot": index + 1, "kind": "upgrade", "id": id, "tier": 0, "can_pick": True}
            for index, id in enumerate(ids)
        ],
    }


def make_runner(clock=None):
    return EconomyRunner(
        EconomyPlanner(KNOWLEDGE),
        output=lambda text: None,
        clock=clock or Clock(),
        ack_timeout_s=2.0,
    )


async def settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class ShopRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_buy_success_records_decision_and_blocks_duplicate(self):
        runner = make_runner()
        server = FakeServer()
        shop = shop_payload()
        await runner.handle_shop(server, shop, context(), 100.0)
        await settle()
        self.assertEqual([kind for kind, _, _ in server.actions], ["shop_buy"])
        self.assertEqual(server.actions[0][1], {"slot": 0})
        layer, action, reason = server.recorder.decisions[0]
        self.assertEqual(layer, "economy")
        self.assertEqual(action["kind"], "shop_buy")
        self.assertIn("购买", reason)
        # 商店推送延迟期间旧视图不重复购买同一槽位（可以离开，但不得重复买）
        await runner.handle_shop(server, shop, context(), 100.0)
        await settle()
        kinds = [kind for kind, _, _ in server.actions]
        self.assertEqual(kinds.count("shop_buy"), 1, "同槽位不得重复购买")

    async def test_serial_no_concurrent_actions(self):
        runner = make_runner()
        server = FakeServer()
        gate = asyncio.Event()

        async def wait_ack(ref, timeout=None):
            await gate.wait()
            return {"ok": True}

        server.wait_ack = wait_ack
        shop = shop_payload()
        await runner.handle_shop(server, shop, context(), 100.0)
        await asyncio.sleep(0)  # 让动作任务跑到 wait_ack
        await runner.handle_shop(server, shop, context(), 100.5)
        self.assertEqual(len(server.actions), 1, "在途动作未回执时不得并发下发")
        gate.set()
        await settle()
        self.assertFalse(runner.busy)

    async def test_insufficient_gold_not_retried(self):
        runner = make_runner()
        server = FakeServer()
        server.acks = {1: {"ok": False, "error": "insufficient_gold"}}
        shop = shop_payload(price=10, reroll_cost=-1)
        await runner.handle_shop(server, shop, context(gold=50), 100.0)
        await settle()
        await runner.handle_shop(server, shop, context(gold=50), 101.1)
        await settle()
        kinds = [kind for kind, _, _ in server.actions]
        self.assertEqual(kinds.count("shop_buy"), 1, "insufficient_gold 不重试")
        self.assertIn("shop_leave", kinds)

    async def test_timeout_retries_once_then_gives_up(self):
        clock = Clock()
        runner = make_runner(clock)
        server = FakeServer()
        server.acks = {1: {"ok": False, "error": "buy_timeout"}, 2: {"ok": False, "error": "buy_timeout"}}
        shop = shop_payload(price=10, reroll_cost=-1)
        await runner.handle_shop(server, shop, context(gold=50), clock.now)
        await settle()
        self.assertEqual([kind for kind, _, _ in server.actions], ["shop_buy"])
        # 冷却内不重试
        await runner.handle_shop(server, shop, context(gold=50), clock.now)
        await settle()
        self.assertEqual(len(server.actions), 1)
        clock.now += 1.1
        await runner.handle_shop(server, shop, context(gold=50), clock.now)
        await settle()
        self.assertEqual([kind for kind, _, _ in server.actions].count("shop_buy"), 2)
        clock.now += 1.1
        await runner.handle_shop(server, shop, context(gold=50), clock.now)
        await settle()
        kinds = [kind for kind, _, _ in server.actions]
        self.assertEqual(kinds.count("shop_buy"), 2, "超时重试仅一次")
        self.assertIn("shop_leave", kinds)

    async def test_cannot_leave_retries_after_cooldown(self):
        clock = Clock()
        runner = make_runner(clock)
        server = FakeServer()
        shop = shop_payload(price=65, slot_id="item_acid", reroll_cost=-1)
        server.acks = {1: {"ok": False, "error": "cannot_leave"}, 2: {"ok": True}}
        await runner.handle_shop(server, shop, context(gold=50), clock.now)
        await settle()
        clock.now += 1.1
        await runner.handle_shop(server, shop, context(gold=50), clock.now)
        await settle()
        kinds = [kind for kind, _, _ in server.actions]
        self.assertEqual(kinds.count("shop_leave"), 2)
        # 成功后同一访问不再重复离开
        clock.now += 1.1
        await runner.handle_shop(server, shop, context(gold=50), clock.now)
        await settle()
        self.assertEqual([kind for kind, _, _ in server.actions].count("shop_leave"), 2)

    async def test_new_visit_resets_suppression(self):
        clock = Clock()
        runner = make_runner(clock)
        server = FakeServer()
        server.acks = {1: {"ok": False, "error": "insufficient_gold"}}
        shop = shop_payload(price=10, reroll_cost=-1)
        await runner.handle_shop(server, shop, context(gold=50), clock.now)
        await settle()
        clock.now += 1.1
        await runner.handle_shop(server, shop, context(gold=50), clock.now)
        await settle()
        # 下一波的商店：槽位重新可买（新访问重置抑制）
        next_shop = shop_payload(wave=3, price=10, reroll_cost=-1)
        await runner.handle_shop(server, next_shop, context(wave=3, gold=50), clock.now)
        await settle()
        kinds = [kind for kind, _, _ in server.actions]
        self.assertEqual(kinds, ["shop_buy", "shop_leave", "shop_buy"])


class LevelUpRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_pick_success_and_no_repick_same_signature(self):
        runner = make_runner()
        server = FakeServer()
        menu = level_up_menu()
        await runner.handle_level_up(server, menu, context(), 100.0)
        await settle()
        self.assertEqual([kind for kind, _, _ in server.actions], ["menu_pick_upgrade"])
        first = server.actions[0][1]
        self.assertEqual(first, {"index": 1})
        # 页关心跳窗口内同签名不重复选（票据 08 观察到的二次 not_in_level_up）
        await runner.handle_level_up(server, menu, context(), 100.5)
        await settle()
        self.assertEqual(len(server.actions), 1)

    async def test_new_signature_allows_new_pick(self):
        runner = make_runner()
        server = FakeServer()
        await runner.handle_level_up(server, level_up_menu(), context(), 100.0)
        await settle()
        menu = level_up_menu(ids=("upgrade_attack_speed_1", "upgrade_ranged_damage_1"))
        await runner.handle_level_up(server, menu, context(), 101.0)
        await settle()
        self.assertEqual(
            [kind for kind, _, _ in server.actions],
            ["menu_pick_upgrade", "menu_pick_upgrade"],
        )

    async def test_pick_timeout_retries_once_then_gives_up(self):
        clock = Clock()
        runner = make_runner(clock)
        server = FakeServer()
        server.acks = {1: {"ok": False, "error": "pick_timeout"}, 2: {"ok": False, "error": "pick_timeout"}}
        menu = level_up_menu()
        await runner.handle_level_up(server, menu, context(), clock.now)
        await settle()
        clock.now += 1.1
        await runner.handle_level_up(server, menu, context(), clock.now)
        await settle()
        clock.now += 1.1
        await runner.handle_level_up(server, menu, context(), clock.now)
        await settle()
        kinds = [kind for kind, _, _ in server.actions]
        self.assertEqual(kinds.count("menu_pick_upgrade"), 2, "超时重试仅一次")

    async def test_bad_index_replans_next_card(self):
        runner = make_runner()
        server = FakeServer()
        server.acks = {1: {"ok": False, "error": "bad_index"}}
        menu = level_up_menu(ids=("upgrade_attack_speed_1", "upgrade_ranged_damage_1"))
        await runner.handle_level_up(server, menu, context(), 100.0)
        await settle()
        await runner.handle_level_up(server, menu, context(), 101.1)
        await settle()
        indexes = [params["index"] for kind, params, _ in server.actions]
        self.assertEqual(len(indexes), 2)
        self.assertNotEqual(indexes[0], indexes[1], "bad_index 后应改选下一张卡")

    async def test_not_in_level_up_marks_done(self):
        runner = make_runner()
        server = FakeServer()
        server.acks = {1: {"ok": False, "error": "not_in_level_up"}}
        menu = level_up_menu()
        await runner.handle_level_up(server, menu, context(), 100.0)
        await settle()
        await runner.handle_level_up(server, menu, context(), 101.1)
        await settle()
        self.assertEqual(len(server.actions), 1)

    async def test_reset_clears_state(self):
        runner = make_runner()
        server = FakeServer()
        server.acks = {1: {"ok": False, "error": "pick_timeout"}}
        await runner.handle_level_up(server, level_up_menu(), context(), 100.0)
        await settle()
        runner.reset()
        await runner.handle_level_up(server, level_up_menu(), context(), 101.0)
        await settle()
        self.assertEqual(len(server.actions), 2)


if __name__ == "__main__":
    unittest.main()
