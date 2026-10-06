"""RunSession 状态机测试：难度交互、开局序列、占位对局动作、终局战报、断线让出。

单元测试用 FakeServer 驱动，另含一个经真实 TCP（FakeMod + IpcServer）的端到端用例。
"""
import asyncio
import random
import time
import unittest

from ab_agent.autopilot import PlaceholderAutopilot
from ab_agent.ipc_server import IpcServer
from ab_agent.run_control import RunController
from ab_agent.session import Phase, RunSession
from ab_agent.state import AgentState

from tests import payloads
from tests.test_ipc_server import QUIET, FakeMod, wait_for


class FakeServer:
    """按 IpcServer.send_action/wait_ack 形状实现的假服务端（只记录动作）。"""

    def __init__(self, state: AgentState, acks=None):
        self.state = state
        self.actions = []
        self.acks = acks or {}
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
        return self.acks.get(ref, {"ok": True})


def make_state(*, connected=True, observe_only=False) -> AgentState:
    state = AgentState()
    if connected:
        state.mark_connected("s-unit", {})
        state.observe_only = observe_only
    return state


def make_session(**kwargs):
    state = make_state()
    server = FakeServer(state)
    lines = []
    session = RunSession(
        state,
        server,
        output=lines.append,
        autopilot=PlaceholderAutopilot(rng=random.Random(1), move_interval_s=0.001),
        replay_path=lambda: r"C:\rec\e2e.ndjson",
        **kwargs,
    )
    return state, server, session, lines


async def settle(session, timeout=1.0) -> bool:
    """等待 STARTING 结束（开局任务自然推进）。"""
    deadline = time.monotonic() + timeout
    while session.phase is Phase.STARTING and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    return session.phase is not Phase.STARTING


def action_kinds(server):
    return [kind for kind, _ in server.actions]


class DifficultyInteractionTest(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_on_difficulty_menu(self):
        state, _, session, lines = make_session()
        state.update_menu(payloads.difficulty())
        await session.tick()
        self.assertIs(session.phase, Phase.AWAIT_INPUT)
        text = "\n".join(lines)
        self.assertIn("检测到难度选择页", text)
        self.assertIn("character_ranger（游侠）", text)
        self.assertIn("D0–D1", text)

    async def test_empty_input_reprints(self):
        state, _, session, lines = make_session()
        state.update_menu(payloads.difficulty())
        await session.tick()
        lines.clear()
        session.handle_input("")
        self.assertIs(session.phase, Phase.AWAIT_INPUT)
        self.assertIn("检测到难度选择页", "\n".join(lines))

    async def test_invalid_and_duplicate_prompt_behavior(self):
        state, _, session, lines = make_session()
        state.update_menu(payloads.difficulty())
        await session.tick()
        lines.clear()
        session.handle_input("abc")
        self.assertIs(session.phase, Phase.AWAIT_INPUT)
        self.assertIn("无法识别", "\n".join(lines))
        session.handle_input("D9")
        self.assertIs(session.phase, Phase.AWAIT_INPUT)
        self.assertIn("超出当前已解锁范围", "\n".join(lines))

    async def test_q_cancels_and_suppresses_same_menu(self):
        state, _, session, lines = make_session()
        state.update_menu(payloads.difficulty())
        await session.tick()
        session.handle_input("q")
        self.assertIs(session.phase, Phase.IDLE)
        lines.clear()
        await session.tick()  # 同一菜单心跳不重复提示
        self.assertIs(session.phase, Phase.IDLE)
        self.assertNotIn("检测到难度选择页", "\n".join(lines))
        state.update_menu(payloads.difficulty(selected="difficulty_1"))
        await session.tick()
        self.assertIs(session.phase, Phase.AWAIT_INPUT)

    async def test_cancel_then_leave_and_reenter_prompts_again(self):
        state, _, session, _ = make_session()
        state.update_menu(payloads.difficulty())
        await session.tick()
        session.handle_input("q")
        state.latest_menu = None  # 离开难度页
        await session.tick()
        state.update_menu(payloads.difficulty())  # 重新进入同一内容页面
        await session.tick()
        self.assertIs(session.phase, Phase.AWAIT_INPUT)

    async def test_observe_only_menu_sets_menu_ready_until_resume(self):
        state, _, session, lines = make_session()
        state.observe_only = True
        state.update_menu(payloads.difficulty())
        await session.tick()
        self.assertIs(session.phase, Phase.MENU_READY)
        self.assertIn("OBSERVE_ONLY", "\n".join(lines))
        state.observe_only = False
        session.on_resume()
        self.assertIs(session.phase, Phase.AWAIT_INPUT)

    async def test_mode_switch_requires_confirmation(self):
        state, server, session, lines = make_session(
            readback_timeout_s=0.05, start_timeout_s=0.05
        )
        state.update_menu(
            payloads.difficulty(modes={"endless": True, "ban": False, "coop": False})
        )
        await session.tick()
        session.handle_input("D0")
        self.assertIs(session.phase, Phase.CONFIRM)
        self.assertIn("无尽模式", "\n".join(lines))
        session.handle_input("n")
        self.assertIs(session.phase, Phase.AWAIT_INPUT)
        session.handle_input("D0")
        self.assertIs(session.phase, Phase.CONFIRM)
        session.handle_input("y")
        self.assertTrue(await settle(session))
        self.assertEqual(action_kinds(server)[0], "menu_set_difficulty")


class StartSequenceTest(unittest.IsolatedAsyncioTestCase):
    def _session_with_hooks(self, **kwargs):
        state, server, session, lines = make_session(**kwargs)

        def on_send(kind, params, ref):
            if kind == "menu_set_difficulty":
                state.update_menu(
                    payloads.difficulty(selected="difficulty_%d" % params["value"])
                )
            elif kind == "menu_start_run":
                state.update_snapshot(payloads.snapshot())

        server.on_send = on_send
        return state, server, session, lines

    async def test_success_path_enters_running(self):
        state, server, session, lines = self._session_with_hooks()
        state.update_menu(payloads.difficulty())
        await session.tick()
        session.handle_input("d1")
        self.assertTrue(await settle(session))
        self.assertIs(session.phase, Phase.RUNNING)
        self.assertEqual(action_kinds(server), ["menu_set_difficulty", "menu_start_run"])
        self.assertEqual(server.actions[0][1], {"value": 1})
        self.assertIn("读回校验通过", "\n".join(lines))

    async def test_ack_failure_returns_to_input(self):
        state, server, session, lines = self._session_with_hooks()
        server.acks = {1: {"ok": False, "error": "bad_difficulty"}}
        state.update_menu(payloads.difficulty())
        await session.tick()
        session.handle_input("D0")
        self.assertTrue(await settle(session))
        self.assertIs(session.phase, Phase.AWAIT_INPUT)
        self.assertIn("bad_difficulty", "\n".join(lines))

    async def test_readback_timeout_returns_to_input(self):
        state, server, session, lines = self._session_with_hooks(readback_timeout_s=0.05)

        def on_send(kind, params, ref):
            if kind == "menu_start_run":
                state.update_snapshot(payloads.snapshot())

        server.on_send = on_send  # 不更新菜单 → 读回失败
        state.update_menu(payloads.difficulty(selected="difficulty_1"))
        await session.tick()
        session.handle_input("D0")
        self.assertTrue(await settle(session))
        self.assertIs(session.phase, Phase.AWAIT_INPUT)
        self.assertEqual(action_kinds(server), ["menu_set_difficulty"])
        self.assertIn("读回校验失败", "\n".join(lines))


class RunLoopTest(unittest.IsolatedAsyncioTestCase):
    async def _started(self, **kwargs):
        state, server, session, lines = make_session(**kwargs)

        def on_send(kind, params, ref):
            if kind == "menu_set_difficulty":
                state.update_menu(
                    payloads.difficulty(selected="difficulty_%d" % params["value"])
                )
            elif kind == "menu_start_run":
                state.update_snapshot(payloads.snapshot())

        server.on_send = on_send
        state.update_menu(payloads.difficulty())
        await session.tick()
        session.handle_input("D0")
        await settle(session)
        server.actions.clear()
        return state, server, session, lines

    async def test_combat_sends_placeholder_moves(self):
        state, server, session, _ = await self._started()
        state.update_snapshot(payloads.snapshot())
        await session.tick()
        self.assertIn("move", action_kinds(server))
        vector = server.actions[0][1]["vector"]
        self.assertEqual(len(vector), 2)

    async def test_shop_leaves_once_within_visit(self):
        state, server, session, lines = await self._started()
        state.update_snapshot(payloads.snapshot(wave_index=None, player=False))
        state.update_shop(payloads.shop(can_leave=True))
        await session.tick()
        await asyncio.sleep(0.02)
        await session.tick()
        await session.tick()
        self.assertEqual(action_kinds(server).count("shop_leave"), 1)
        self.assertTrue(any("固定动作：离开" in line for line in lines))

    async def test_shop_waits_for_can_leave(self):
        state, server, session, _ = await self._started()
        state.update_snapshot(payloads.snapshot(wave_index=None, player=False))
        state.update_shop(payloads.shop(can_leave=False))
        await session.tick()
        await asyncio.sleep(0.01)
        self.assertNotIn("shop_leave", action_kinds(server))

    async def test_level_up_picks_first_pickable(self):
        state, server, session, _ = await self._started()
        state.update_menu(payloads.level_up())
        await session.tick()
        await asyncio.sleep(0.02)
        self.assertEqual(action_kinds(server), ["menu_pick_upgrade"])
        self.assertEqual(server.actions[0][1], {"index": 1})

    async def test_run_end_prints_report_then_returns_idle(self):
        state, server, session, lines = await self._started()
        state.update_snapshot(payloads.snapshot(gold=321, materials=15))
        await session.tick()
        state.update_menu(payloads.run_end(result="victory", wave=20))
        await session.tick()
        self.assertIs(session.phase, Phase.ENDED)
        report = "\n".join(lines)
        self.assertIn("对局战报", report)
        self.assertIn("胜利", report)
        self.assertIn("第 20 波", report)
        self.assertIn("金币：321", report)
        self.assertIn("weapon_pistol(T1)", report)
        self.assertIn("item_helmet×2", report)
        self.assertIn(r"C:\rec\e2e.ndjson", report)
        lines.clear()
        await session.tick()  # ENDED → IDLE；run_end 心跳不重复打印
        self.assertIs(session.phase, Phase.IDLE)
        self.assertIn("已回到待命", "\n".join(lines))
        self.assertNotIn("对局战报", "\n".join(lines))

    async def test_death_fallback_prints_defeat(self):
        state, server, session, lines = await self._started(death_fallback_s=0.05)
        state.latest_menu = None  # 对局中残留的难度页观测不应再触发提示（模拟真实离场）
        state.update_snapshot(payloads.snapshot(alive=False))
        await session.tick()
        await asyncio.sleep(0.06)
        await session.tick()
        report = "\n".join(lines)
        self.assertIn("战败", report)
        self.assertIn("死亡兜底", report)
        self.assertIs(session.phase, Phase.ENDED)
        await session.tick()
        self.assertIs(session.phase, Phase.IDLE)

    async def test_disconnect_gates_moves_until_resume(self):
        state, server, session, _ = await self._started()
        state.update_snapshot(payloads.snapshot())
        await session.tick()
        self.assertGreater(action_kinds(server).count("move"), 0)
        state.mark_disconnected()
        server.actions.clear()
        await session.tick()
        self.assertEqual(action_kinds(server), [], "断线后不得再发 move")
        state.mark_connected("s-again", {})  # 重连默认 OBSERVE_ONLY
        state.update_snapshot(payloads.snapshot())
        await session.tick()
        self.assertEqual(action_kinds(server), [], "重连后未 resume 不得发 move")
        state.observe_only = False
        session.on_resume()
        await session.tick()
        self.assertIn("move", action_kinds(server), "resume 后恢复接管")


class ControllerAccountingTest(unittest.TestCase):
    def test_begin_clears_previous_run_report_data(self):
        state = make_state()
        controller = RunController(
            FakeServer(state), clock=time.monotonic, ack_timeout_s=1.0
        )
        controller.begin()
        controller.observe(payloads.snapshot(gold=999, materials=12))
        report = controller.build_report(None, duration_s=None)
        self.assertEqual(report.gold, 999)
        self.assertEqual(report.materials, 12)
        controller.begin()
        report = controller.build_report(None, duration_s=None)
        self.assertIsNone(report.gold)
        self.assertIsNone(report.materials)
        self.assertEqual(report.weapons, ())


class EndToEndFlowTest(unittest.IsolatedAsyncioTestCase):
    """真实 TCP 链路 + FakeMod 的端到端：难度页 → 开局 → 商店 → 升级 → 终局。"""

    async def asyncSetUp(self):
        self.state = AgentState()
        self.lines = []
        self.server = IpcServer(
            self.state, host="127.0.0.1", port=0, ping_interval=0, logger=QUIET
        )
        await self.server.start()
        self.addAsyncCleanup(self.server.shutdown)
        self.session = RunSession(
            self.state,
            self.server,
            output=self.lines.append,
            autopilot=PlaceholderAutopilot(rng=random.Random(7), move_interval_s=0.05),
            replay_path=lambda: r"C:\rec\tcp.ndjson",
        )
        self.tick_task = asyncio.create_task(self._tick_forever())

        async def cancel():
            self.tick_task.cancel()
            try:
                await self.tick_task
            except asyncio.CancelledError:
                pass

        self.addAsyncCleanup(cancel)

    async def _tick_forever(self):
        while True:
            await self.session.tick()
            await asyncio.sleep(0.02)

    async def recv_action(self, mod, kind, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            envelope = await mod.recv(timeout=max(0.1, deadline - time.monotonic()))
            if envelope is None:
                return None
            if (
                envelope.get("type") == "action"
                and envelope.get("payload", {}).get("kind") == kind
            ):
                return envelope
        return None

    async def test_full_flow(self):
        mod = await FakeMod.connect(self.server)
        welcome = await mod.hello()
        self.assertEqual(welcome["type"], "welcome")
        self.assertFalse(self.state.observe_only, "首连接管")
        await wait_for(lambda: self.session.phase is Phase.IDLE)

        await mod.send("menu", payloads.difficulty())
        await wait_for(lambda: self.session.phase is Phase.AWAIT_INPUT)

        self.session.handle_input("D9")
        self.assertIs(self.session.phase, Phase.AWAIT_INPUT)
        self.session.handle_input("abc")
        self.assertIs(self.session.phase, Phase.AWAIT_INPUT)

        self.session.handle_input("D1")
        envelope = await self.recv_action(mod, "menu_set_difficulty")
        self.assertIsNotNone(envelope)
        self.assertEqual(envelope["payload"]["value"], 1)
        await mod.send("ack", {"ok": True}, ref=envelope["ref"])
        await mod.send("menu", payloads.difficulty(selected="difficulty_1"))

        envelope = await self.recv_action(mod, "menu_start_run")
        self.assertIsNotNone(envelope)
        await mod.send("ack", {"ok": True}, ref=envelope["ref"])
        await mod.send("snapshot", payloads.snapshot())
        self.assertTrue(await wait_for(lambda: self.session.phase is Phase.RUNNING))

        envelope = await self.recv_action(mod, "move")
        self.assertIsNotNone(envelope, "对局中应下发占位 move")

        await mod.send("snapshot", payloads.snapshot(wave_index=None, player=False))
        await mod.send("shop", payloads.shop(can_leave=True))
        envelope = await self.recv_action(mod, "shop_leave")
        self.assertIsNotNone(envelope)
        await mod.send("ack", {"ok": True}, ref=envelope["ref"])

        await mod.send("menu", payloads.level_up())
        envelope = await self.recv_action(mod, "menu_pick_upgrade")
        self.assertIsNotNone(envelope)
        self.assertEqual(envelope["payload"]["index"], 1)
        await mod.send("ack", {"ok": True}, ref=envelope["ref"])

        await mod.send("menu", payloads.run_end(result="defeat", wave=6))
        self.assertTrue(await wait_for(lambda: self.session.phase is Phase.IDLE))
        report = "\n".join(self.lines)
        self.assertIn("战败", report)
        self.assertIn("第 6 波", report)
        self.assertIn("回放：C:\\rec\\tcp.ndjson", report)
        mod.close()


if __name__ == "__main__":
    unittest.main()
