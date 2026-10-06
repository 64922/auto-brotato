"""经济层接线测试：RunSession/RunController 用评分决策并经 recorder 落盘 decision。"""
import asyncio
import random
import time
import unittest

from ab_agent.autopilot import PlaceholderAutopilot
from ab_agent.decision.economy import EconomyPlanner
from ab_agent.decision.economy_runner import EconomyRunner
from ab_agent.knowledge import load_knowledge
from ab_agent.run_control import RunController
from ab_agent.session import Phase, RunSession
from ab_agent.state import AgentState

from tests import payloads

KNOWLEDGE = load_knowledge()


class RecordingServer:
    """按 IpcServer 形状实现的假服务端：记录动作并暴露 recorder。"""

    def __init__(self, state: AgentState):
        self.state = state
        self.actions = []
        self.recorder = _Recorder()
        self.on_send = None

    async def send_action(self, kind, params=None, *, track_ack=False):
        if not self.state.connected or self.state.observe_only:
            return None
        ref = len(self.actions) + 1
        params = dict(params or {})
        self.actions.append((kind, params))
        if self.on_send is not None:
            self.on_send(kind, params, ref)
        return ref

    async def wait_ack(self, ref, timeout=None):
        await asyncio.sleep(0)
        return {"ok": True}


class _Recorder:
    def __init__(self):
        self.decisions = []

    def record_decision(self, layer, action, reason):
        self.decisions.append((layer, action, reason))


class SpyPlanner(EconomyPlanner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.contexts = []

    def plan_shop(self, shop, context, **kwargs):
        self.contexts.append(context)
        return super().plan_shop(shop, context, **kwargs)


def make_session(planner):
    state = AgentState()
    state.mark_connected("s-eco", {})
    state.observe_only = False
    server = RecordingServer(state)
    lines = []
    economy = EconomyRunner(
        planner, output=lines.append, clock=time.monotonic, ack_timeout_s=2.0
    )
    session = RunSession(
        state,
        server,
        output=lines.append,
        autopilot=PlaceholderAutopilot(rng=random.Random(1), move_interval_s=0.001),
        economy=economy,
        replay_path=lambda: None,
    )
    return state, server, session, lines, economy


async def enter_running(session, state, server):
    def on_send(kind, params, ref):
        if kind == "menu_set_difficulty":
            state.update_menu(payloads.difficulty(selected="difficulty_%d" % params["value"]))
        elif kind == "menu_start_run":
            state.update_snapshot(payloads.snapshot())

    server.on_send = on_send
    state.update_menu(payloads.difficulty())
    await session.tick()
    session.handle_input("D0")
    deadline = time.monotonic() + 1.0
    while session.phase is Phase.STARTING and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    server.actions.clear()


def economy_shop():
    return {
        "wave_next": 2,
        "gold": 50,
        "slots": [
            {
                "slot": 0,
                "kind": "weapon",
                "id": "weapon_smg_1",
                "tier": 0,
                "price": 20,
                "sold": False,
                "locked": False,
            }
        ],
        "inventory": {"weapons": [], "items": []},
        "stats": {"ranged_damage": 0},
        "reroll": {"cost": 3, "count": 0},
        "can_leave": True,
    }


class EconomyWiringTest(unittest.IsolatedAsyncioTestCase):
    async def test_shop_buys_via_planner_with_hero_context(self):
        planner = SpyPlanner(KNOWLEDGE)
        state, server, session, lines, _ = make_session(planner)
        await enter_running(session, state, server)
        state.update_snapshot(payloads.snapshot(wave_index=None, player=False))
        state.update_shop(economy_shop())
        await session.tick()
        await asyncio.sleep(0.02)
        kinds = [kind for kind, _ in server.actions]
        self.assertIn("shop_buy", kinds, "\n".join(lines))
        self.assertEqual(planner.contexts[0].hero_id, "character_ranger")
        layer, action, reason = server.recorder.decisions[0]
        self.assertEqual(layer, "economy")
        self.assertEqual(action["kind"], "shop_buy")
        self.assertIn("评分", reason)

    async def test_level_up_picks_scored_card(self):
        planner = EconomyPlanner(KNOWLEDGE)
        state, server, session, lines, _ = make_session(planner)
        await enter_running(session, state, server)
        state.update_snapshot(payloads.snapshot())
        menu = payloads.level_up()
        state.update_menu(menu)
        await session.tick()
        await asyncio.sleep(0.02)
        picks = [params for kind, params in server.actions if kind == "menu_pick_upgrade"]
        self.assertEqual(len(picks), 1, "\n".join(lines))
        expected = planner.plan_level_up(
            menu,
            # payloads.level_up 的 ranged_damage 在第 3 张：低于短板权重的 card 也会被显式评分
            _context_from_snapshot(state),
        )
        self.assertEqual(picks[0]["index"], expected.slot)
        self.assertTrue(
            any("economy" == layer for layer, _, _ in server.recorder.decisions)
        )

    async def test_level_up_without_tick_state_uses_defaults(self):
        """无快照时上下文退化为空统计，仍能选卡（不抛异常）。"""
        planner = EconomyPlanner(KNOWLEDGE)
        state, server, session, lines, _ = make_session(planner)
        await enter_running(session, state, server)
        state.latest_snapshot = None
        state.update_menu(payloads.level_up())
        await session.tick()
        await asyncio.sleep(0.02)
        self.assertTrue(
            any(kind == "menu_pick_upgrade" for kind, _ in server.actions),
            "\n".join(lines),
        )

    async def test_controller_without_economy_keeps_fallback(self):
        state = AgentState()
        state.mark_connected("s-fallback", {})
        state.observe_only = False
        server = RecordingServer(state)
        controller = RunController(
            server, output=lambda text: None, clock=time.monotonic, ack_timeout_s=2.0
        )
        controller.begin()
        state.update_snapshot(payloads.snapshot(wave_index=None, player=False))
        state.update_shop(payloads.shop(can_leave=True))
        await controller.tick(state, None, time.monotonic())
        await asyncio.sleep(0.02)
        self.assertEqual([kind for kind, _ in server.actions], ["shop_leave"])


def _context_from_snapshot(state):
    from ab_agent.decision.economy_model import EconomyContext

    snapshot = state.latest_snapshot or {}
    return EconomyContext(
        wave=snapshot.get("wave", {}).get("index") or 1,
        gold=(snapshot.get("economy") or {}).get("gold") or 0,
        stats=snapshot.get("stats") or {},
        inventory=snapshot.get("inventory") or {},
        hero_id="character_ranger",
    )


if __name__ == "__main__":
    unittest.main()
