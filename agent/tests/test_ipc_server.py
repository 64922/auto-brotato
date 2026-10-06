"""ipc_server.py 集成测试：用 asyncio 假 mod 客户端驱动真实 TCP 链路。"""
import asyncio
import json
import logging
import time
import unittest

from ab_agent.ipc_server import IpcServer
from ab_agent.protocol import GAME_VERSION, PROTOCOL_VERSION
from ab_agent.state import AgentState

QUIET = logging.getLogger("tests.ipc")
QUIET.addHandler(logging.NullHandler())
QUIET.propagate = False


async def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


class FakeMod:
    """最简 mod 侧客户端（NDJSON 信封）。"""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.seq = 0

    @classmethod
    async def connect(cls, server):
        reader, writer = await asyncio.open_connection(server.host, server.bound_port)
        return cls(reader, writer)

    async def send(self, msg_type, payload=None, ref=None):
        envelope = {
            "v": PROTOCOL_VERSION,
            "seq": self.seq,
            "ts": time.time(),
            "type": msg_type,
            "ref": ref,
            "payload": {} if payload is None else payload,
        }
        self.seq += 1
        self.writer.write((json.dumps(envelope, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.writer.drain()

    async def send_raw(self, text):
        self.writer.write(text.encode("utf-8"))
        await self.writer.drain()

    async def recv(self, timeout=2.0):
        """读一条消息；连接关闭返回 None。"""
        try:
            line = await asyncio.wait_for(self.reader.readline(), timeout)
        except ConnectionError:
            return None
        if not line:
            return None
        return json.loads(line.decode("utf-8"))

    async def hello(
        self,
        *,
        protocol_version=PROTOCOL_VERSION,
        game_version=GAME_VERSION,
        session_id="test-session",
        mod_version="0.2.0",
    ):
        await self.send(
            "hello",
            {
                "protocol_version": protocol_version,
                "mod_version": mod_version,
                "game_version": game_version,
                "session_id": session_id,
                "capabilities": {"snapshot_hz": 60, "move_analog": True},
            },
        )
        return await self.recv()

    def close(self):
        self.writer.close()


class IpcServerTestBase(unittest.IsolatedAsyncioTestCase):
    ping_interval = 0

    async def asyncSetUp(self):
        self.state = AgentState()
        self.server = IpcServer(
            self.state,
            host="127.0.0.1",
            port=0,
            ping_interval=self.ping_interval,
            logger=QUIET,
        )
        await self.server.start()
        self.addAsyncCleanup(self.server.shutdown)

    async def handshake(self, mod=None, **kwargs):
        mod = mod or await FakeMod.connect(self.server)
        welcome = await mod.hello(**kwargs)
        assert welcome is not None and welcome["type"] == "welcome", welcome
        return mod, welcome


class HandshakeTest(IpcServerTestBase):
    async def test_handshake_sends_welcome_and_updates_state(self):
        mod, welcome = await self.handshake()
        payload = welcome["payload"]
        self.assertEqual(payload["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(payload["session_id"], "test-session")
        self.assertEqual(
            payload["config"],
            {"snapshot_hz": 60, "debug_overlay": False, "action_ttl_ms": 250},
        )
        self.assertTrue(self.state.connected)
        self.assertEqual(self.state.session_id, "test-session")
        self.assertEqual(self.state.hello["mod_version"], "0.2.0")
        mod.close()

    async def test_protocol_version_mismatch_rejects_and_closes(self):
        mod = await FakeMod.connect(self.server)
        error = await mod.hello(protocol_version=1, mod_version="0.1.0")
        self.assertEqual(error["type"], "error")
        self.assertEqual(error["payload"]["code"], "version_mismatch")
        self.assertIn("protocol_version", error["payload"]["message"])
        self.assertIsNone(await mod.recv(), "拒绝会话后服务端应关闭连接")
        self.assertFalse(self.state.connected)
        self.assertEqual(self.state.protocol_errors, 1)

    async def test_game_version_mismatch_rejects_and_closes(self):
        mod = await FakeMod.connect(self.server)
        error = await mod.hello(game_version="1.1.15.5")
        self.assertEqual(error["type"], "error")
        self.assertIn("game_version", error["payload"]["message"])
        self.assertIsNone(await mod.recv())
        self.assertFalse(self.state.connected)

    async def test_message_before_handshake_is_ignored(self):
        mod = await FakeMod.connect(self.server)
        await mod.send("snapshot", {"t": 1.0})
        self.assertIsNone(self.state.latest_snapshot)
        self.assertTrue(await wait_for(lambda: self.state.protocol_errors == 1))
        await mod.hello()
        self.assertTrue(self.state.connected)
        mod.close()


class ObservationTest(IpcServerTestBase):
    async def test_snapshot_updates_state_frequency_and_wave_summary(self):
        mod, _ = await self.handshake()
        for index in range(20):
            await mod.send(
                "snapshot",
                {
                    "t": index / 100.0,
                    "wave": {"index": 3, "phase": "combat", "time_left": 12.0},
                    "player": {"alive": True, "hp": 15},
                },
            )
            await asyncio.sleep(0.005)
        self.assertTrue(await wait_for(lambda: self.state.snapshot_count == 20))
        self.assertGreater(self.state.snapshot_hz(), 0.0)
        self.assertIn("第 3 波", self.state.wave_summary())
        self.assertIn("第 3 波", self.state.describe())
        mod.close()

    async def test_shop_and_event_are_recorded(self):
        mod, _ = await self.handshake()
        await mod.send("shop", {"wave_next": 2, "gold": 30, "slots": []})
        await mod.send("event", {"name": "purchase_done", "data": {"slot": 1}})
        self.assertTrue(await wait_for(lambda: self.state.latest_shop is not None))
        self.assertTrue(await wait_for(lambda: self.state.last_event is not None))
        self.assertEqual(self.state.latest_shop["wave_next"], 2)
        self.assertEqual(self.state.last_event["name"], "purchase_done")
        mod.close()

    async def test_menu_updates_state(self):
        mod, _ = await self.handshake()
        await mod.send("menu", {"phase": "difficulty_select", "can_start": True})
        self.assertTrue(await wait_for(lambda: self.state.latest_menu is not None))
        self.assertEqual(self.state.latest_menu["phase"], "difficulty_select")
        self.assertEqual(self.state.menu_count, 1)
        self.assertIsNotNone(self.state.menu_age())
        mod.close()

    async def test_malformed_line_ignored_without_dropping_link(self):
        mod, _ = await self.handshake()
        await mod.send_raw("{broken json\n")
        await mod.send("snapshot", {"t": 1.0, "wave": None, "player": None})
        self.assertTrue(await wait_for(lambda: self.state.snapshot_count == 1))
        self.assertEqual(self.state.protocol_errors, 1)
        self.assertTrue(self.state.connected)
        mod.close()


class SingleClientTest(IpcServerTestBase):
    async def test_second_connection_replaces_first(self):
        mod_a, _ = await self.handshake(session_id="session-a")
        mod_b = await FakeMod.connect(self.server)
        self.assertIsNone(await mod_a.recv(), "旧连接应被服务端关闭")
        welcome = await mod_b.hello(session_id="session-b")
        self.assertEqual(welcome["payload"]["session_id"], "session-b")
        self.assertEqual(self.state.session_id, "session-b")
        await mod_b.send("snapshot", {"t": 1.0, "wave": None, "player": None})
        self.assertTrue(await wait_for(lambda: self.state.snapshot_count == 1))
        mod_b.close()

    async def test_reconnect_defaults_to_observe_only(self):
        mod, _ = await self.handshake()
        self.assertFalse(self.state.observe_only, "首次连接即接管")
        mod.close()
        self.assertTrue(await wait_for(lambda: not self.state.connected))
        mod_again = await FakeMod.connect(self.server)
        await mod_again.hello(session_id="session-again")
        self.assertTrue(self.state.observe_only, "重连默认 OBSERVE_ONLY")
        mod_again.close()


class ActionTest(IpcServerTestBase):
    async def test_action_rejected_when_not_connected(self):
        ref = await self.server.send_action("debug_overlay", {"enabled": True})
        self.assertIsNone(ref)

    async def test_action_rejected_in_observe_only(self):
        mod, _ = await self.handshake()
        self.state.observe_only = True
        ref = await self.server.send_action("debug_overlay", {"enabled": True})
        self.assertIsNone(ref)
        with self.assertRaises(asyncio.TimeoutError):
            await mod.recv(timeout=0.2)
        mod.close()

    async def test_action_ref_increments_and_ack_matches(self):
        mod, _ = await self.handshake()
        self.state.observe_only = False
        ref1 = await self.server.send_action("debug_overlay", {"enabled": True}, track_ack=True)
        envelope1 = await mod.recv()
        self.assertEqual(envelope1["type"], "action")
        self.assertEqual(envelope1["payload"]["kind"], "debug_overlay")
        self.assertEqual(envelope1["ref"], ref1)
        await mod.send("ack", {"ok": True}, ref=ref1)
        ack = await self.server.wait_ack(ref1, timeout=1.0)
        self.assertEqual(ack, {"ok": True})

        ref2 = await self.server.send_action("move", {"vector": [1, 0]})
        envelope2 = await mod.recv()
        self.assertEqual(envelope2["ref"], ref2)
        self.assertEqual(ref2, ref1 + 1)
        mod.close()

    async def test_failed_ack_reaches_waiter(self):
        mod, _ = await self.handshake()
        self.state.observe_only = False
        ref = await self.server.send_action("shop_buy", {"slot": 1}, track_ack=True)
        await mod.recv()
        await mod.send("ack", {"ok": False, "error": "insufficient_gold"}, ref=ref)
        ack = await self.server.wait_ack(ref, timeout=1.0)
        self.assertEqual(ack, {"ok": False, "error": "insufficient_gold"})
        mod.close()

    async def test_pending_action_fails_on_disconnect(self):
        mod, _ = await self.handshake()
        self.state.observe_only = False
        ref = await self.server.send_action("shop_buy", {"slot": 1}, track_ack=True)
        await mod.recv()
        mod.close()
        ack = await self.server.wait_ack(ref, timeout=1.0)
        self.assertIsNone(ack, "断连后在途动作应判定失败")


class HeartbeatTest(IpcServerTestBase):
    ping_interval = 0.05

    async def test_ping_and_pong_updates_link(self):
        mod, _ = await self.handshake()
        envelope = await mod.recv(timeout=1.0)
        self.assertEqual(envelope["type"], "ping")
        await mod.send("pong", {})
        self.assertTrue(await wait_for(lambda: self.state.last_pong_at is not None))
        mod.close()


class StreamLimitTest(IpcServerTestBase):
    async def test_large_snapshot_line_beyond_default_limit_is_parsed(self):
        mod, _ = await self.handshake()
        big = "x" * (100 * 1024)
        await mod.send("snapshot", {"t": 1.0, "wave": None, "player": None, "note": big})
        self.assertTrue(await wait_for(lambda: self.state.snapshot_count == 1))
        self.assertEqual(self.state.protocol_errors, 0)
        mod.close()


class HangingWriter:
    """对端停止读取：drain 永不返回，用于验证发送有上限、不无限挂起。"""

    def is_closing(self):
        return False

    def write(self, data):
        return None

    async def drain(self):
        await asyncio.sleep(30)


class DrainTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_send_gives_up_when_drain_hangs(self):
        from unittest import mock

        from ab_agent.ipc_server import ClientConnection

        conn = ClientConnection(None, HangingWriter(), "fake", recorder=None)
        with mock.patch("ab_agent.ipc_server.SEND_DRAIN_TIMEOUT_S", 0.05):
            start = time.monotonic()
            sent = await conn.send("action", {"kind": "move"}, ref=1)
        self.assertFalse(sent, "drain 挂起应判定发送失败")
        self.assertLess(time.monotonic() - start, 1.0)


if __name__ == "__main__":
    unittest.main()
