"""asyncio TCP 服务端：mod 主动连接，NDJSON 信封（docs/protocol.md）。

职责：监听端口、握手与版本校验（ADR-0005）、消息分发到 AgentState、
下发 action 并维护 ref/ack、心跳 ping、断连检测与日志。
单客户端假设：新连接替换旧连接（告警）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from . import __version__, protocol
from .protocol import Envelope, ProtocolError
from .recorder import Recorder
from .state import AgentState

LOGGER_NAME = "ab.ipc"

#: NDJSON 单行上限：快照可含 300 敌 + 400 弹幕，需大于 asyncio 默认 64KiB
STREAM_LIMIT = 8 * 1024 * 1024

#: agent 侧动作未回执判定（protocol.md §5.2 第 4 条，双保险 2s）
ACTION_ACK_TIMEOUT_S = 2.0

#: 默认心跳间隔（mod 侧 2s 无 agent 消息则判定链路失效并停住）
DEFAULT_PING_INTERVAL_S = 1.0


class ClientConnection:
    """单个 mod 连接（mod 为 TCP 客户端）。"""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: str,
        recorder: Recorder | None = None,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.peer = peer
        self.session_id: Optional[str] = None
        self.hello: Optional[dict] = None
        self.ready = False
        self.rejected = False
        self.connected_at = time.monotonic()
        self.last_recv_at = self.connected_at
        self._recorder = recorder
        self._seq = 0

    def next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    async def send(self, msg_type: str, payload: dict | None = None, ref: Any = None) -> bool:
        """发送一条信封；连接失效返回 False（不抛异常）。

        录制 out 只覆盖动作（welcome/ping/error 不入录制，见 recorder.py 格式说明）。
        """
        if self.writer.is_closing() or self.rejected:
            return False
        seq = self.next_seq()
        ts = time.time()
        data = protocol.encode(msg_type, payload, seq=seq, ref=ref, ts=ts)
        try:
            self.writer.write(data)
            await self.writer.drain()
        except (ConnectionError, OSError):
            return False
        if self._recorder is not None and msg_type == "action":
            self._recorder.record_out(
                {
                    "v": protocol.PROTOCOL_VERSION,
                    "seq": seq,
                    "ts": ts,
                    "type": msg_type,
                    "ref": ref,
                    "payload": {} if payload is None else payload,
                }
            )
        return True


class IpcServer:
    """监听 127.0.0.1:37650 的协议服务端骨架（不含决策逻辑）。"""

    def __init__(
        self,
        state: AgentState,
        *,
        host: str = "127.0.0.1",
        port: int = 37650,
        ping_interval: float = DEFAULT_PING_INTERVAL_S,
        logger: logging.Logger | None = None,
        recorder: Recorder | None = None,
    ) -> None:
        self.state = state
        self.host = host
        self.port = port
        self.log = logger or logging.getLogger(LOGGER_NAME)
        self.recorder = recorder
        self._ping_interval = ping_interval
        self._server: Optional[asyncio.AbstractServer] = None
        self._client: Optional[ClientConnection] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._pending: dict[Any, dict] = {}
        self._ref_counter = 0

    # ---- 生命周期 ----

    async def start(self) -> "IpcServer":
        self._server = await asyncio.start_server(
            self._handle_client,
            self.host,
            self.port,
            limit=STREAM_LIMIT,
        )
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="ab-heartbeat"
        )
        return self

    async def shutdown(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        client = self._client
        if client is not None:
            client.writer.close()
            self._client = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._fail_pending("shutdown")
        self.state.mark_disconnected()
        if self.recorder is not None:
            self.recorder.end_session()

    @property
    def bound_port(self) -> int:
        """实际监听端口（port=0 时由系统分配）。"""
        if self._server is not None and self._server.sockets:
            return self._server.sockets[0].getsockname()[1]
        return self.port

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.ready

    # ---- 对外动作接口 ----

    def disconnect_client(self, reason: str = "用户 stop 让出控制") -> bool:
        """主动断开当前连接（mod 会退避重连，重连后保持 OBSERVE_ONLY）。"""
        client = self._client
        if client is None:
            self.log.info("当前无活动连接，无需断开（%s）", reason)
            return False
        self.log.info("主动断开连接（%s）：%s", reason, client.peer)
        client.writer.close()
        return True

    async def send_action(
        self,
        kind: str,
        params: dict | None = None,
        *,
        track_ack: bool = False,
    ) -> Any:
        """下发动作；未连接或 OBSERVE_ONLY 时拒绝并返回 None。"""
        client = self._client
        if client is None or not client.ready:
            self.log.info("未连接，动作未发送：kind=%s", kind)
            return None
        if self.state.observe_only:
            self.log.info("OBSERVE_ONLY：动作未发送（resume 后恢复接管）：kind=%s", kind)
            return None
        self._ref_counter += 1
        ref = self._ref_counter
        if track_ack:
            future = asyncio.get_running_loop().create_future()
            self._pending[ref] = {"future": future, "kind": kind, "sent_at": time.monotonic()}
        payload: dict = {"kind": kind}
        if params:
            payload.update(params)
        if not await client.send("action", payload, ref=ref):
            self._pending.pop(ref, None)
            self.log.warning("动作发送失败：kind=%s ref=%s", kind, ref)
            return None
        self.log.debug("已发送动作：kind=%s ref=%s", kind, ref)
        return ref

    async def wait_ack(self, ref: Any, timeout: float | None = None) -> Optional[dict]:
        """等待动作回执；超时/断连返回 None。"""
        pending = self._pending.get(ref)
        if pending is None:
            return None
        timeout = ACTION_ACK_TIMEOUT_S if timeout is None else timeout
        try:
            result = await asyncio.wait_for(asyncio.shield(pending["future"]), timeout)
        except asyncio.TimeoutError:
            self._pending.pop(ref, None)
            self.log.warning(
                "动作未回执（超过 %.1fs）：ref=%s kind=%s", timeout, ref, pending["kind"]
            )
            return None
        return result

    # ---- 连接处理 ----

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer_info = writer.get_extra_info("peername")
        peer = "%s:%s" % (peer_info[0], peer_info[1]) if peer_info else "?"
        conn = ClientConnection(reader, writer, peer, recorder=self.recorder)
        old = self._client
        if old is not None:
            self.log.warning(
                "已有活动连接 %s，被新连接 %s 替换（单客户端假设）", old.peer, peer
            )
            old.writer.close()
            self._client = None
            self.state.mark_disconnected()
            self._fail_pending("link_lost")
        self._client = conn
        self.log.info("mod 已连接：%s", peer)
        try:
            await self._pump(conn)
        finally:
            if self._client is conn:
                self._client = None
                self.state.mark_disconnected()
                self._fail_pending("link_lost")
            if self.recorder is not None:
                self.recorder.end_session(owner=conn)
            if conn.ready:
                self.log.info(
                    "会话结束：session=%s（连接时长 %.1fs）",
                    conn.session_id,
                    time.monotonic() - conn.connected_at,
                )
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self.log.info("连接已关闭：%s", peer)

    async def _pump(self, conn: ClientConnection) -> None:
        while True:
            try:
                line = await conn.reader.readline()
            except ValueError:
                self.state.note_protocol_error()
                self.log.error("单行超过上限（%d 字节），断开连接：%s", STREAM_LIMIT, conn.peer)
                return
            except (ConnectionError, OSError) as exc:
                self.log.info("连接读取错误：%s（%s）", conn.peer, exc)
                return
            if not line:
                self.log.info("mod 断开连接：%s", conn.peer)
                return
            if self._client is not conn:
                self.log.info("连接已被新连接替换，停止处理旧连接消息：%s", conn.peer)
                return
            conn.last_recv_at = time.monotonic()
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                envelope = protocol.parse_line(text)
            except ProtocolError as exc:
                self.state.note_protocol_error()
                self.log.warning("丢弃无法解析的消息：%s（%s）", exc, text[:200])
                continue
            if not await self._dispatch(conn, envelope):
                return

    async def _dispatch(self, conn: ClientConnection, envelope: Envelope) -> bool:
        """分发一条消息；返回 False 表示结束该连接。"""
        msg_type = envelope.type
        if msg_type == "hello":
            if conn.ready:
                self.log.warning("重复 hello，已忽略：%s", conn.peer)
                return True
            return await self._handshake(conn, envelope)
        if not conn.ready:
            self.state.note_protocol_error()
            self.log.warning("握手完成前收到 %s，已忽略：%s", msg_type, conn.peer)
            return True
        if self.recorder is not None:
            self.recorder.record_in(envelope.as_dict())
        payload = envelope.payload
        if msg_type == "snapshot":
            self.state.update_snapshot(payload)
            if self.state.snapshot_count == 1:
                self.log.info("首条快照：%s", self.state.wave_summary())
            return True
        if msg_type == "shop":
            self.state.update_shop(payload)
            self.log.debug("shop 更新：wave_next=%s", payload.get("wave_next"))
            return True
        if msg_type == "menu":
            self.state.update_menu(payload)
            self.log.debug("menu 更新：phase=%s", payload.get("phase"))
            return True
        if msg_type == "event":
            self.state.note_event(payload)
            self.log.info("事件：%s %s", payload.get("name"), payload.get("data", {}))
            return True
        if msg_type == "ack":
            self._resolve_ack(envelope)
            return True
        if msg_type == "pong":
            self.state.note_pong()
            return True
        self.log.warning("未知消息类型（已忽略）：%s", msg_type)
        return True

    async def _handshake(self, conn: ClientConnection, envelope: Envelope) -> bool:
        payload = envelope.payload
        error = protocol.check_hello(payload)
        session_id = payload.get("session_id")
        if error:
            await conn.send("error", protocol.error_payload("version_mismatch", error))
            conn.rejected = True
            self.state.note_protocol_error()
            self.log.error(
                "拒绝会话：%s（mod=%s game=%s session=%s，连接 %s）",
                error,
                payload.get("mod_version"),
                payload.get("game_version"),
                session_id,
                conn.peer,
            )
            return False
        conn.session_id = session_id
        conn.hello = payload
        if not await conn.send("welcome", protocol.welcome_payload(session_id)):
            self.log.warning("welcome 发送失败：%s", conn.peer)
            return False
        conn.ready = True
        self.state.mark_connected(session_id, payload)
        if self.recorder is not None:
            self.recorder.start_session(
                hello=payload, agent_version=__version__, owner=conn
            )
            self.recorder.record_in(envelope.as_dict())
        self.log.info(
            "握手完成：session=%s mod=%s game=%s protocol=%s",
            session_id,
            payload.get("mod_version"),
            payload.get("game_version"),
            payload.get("protocol_version"),
        )
        return True

    # ---- ack 匹配与心跳 ----

    def _resolve_ack(self, envelope: Envelope) -> None:
        ref = envelope.ref
        payload = envelope.payload
        self.state.note_ack(ref, payload)
        pending = self._pending.pop(ref, None)
        if pending is not None and not pending["future"].done():
            pending["future"].set_result(payload)
        if payload.get("ok"):
            self.log.debug("收到 ack：ref=%s", ref)
        else:
            self.log.warning("动作失败：ref=%s error=%s", ref, payload.get("error"))
        # 未经 wait_ack/track_ack 的 ack（如 60Hz move 回执）只记账，不产生未处理 future

    def _reap_pending(self) -> None:
        now = time.monotonic()
        for ref, pending in list(self._pending.items()):
            if now - pending["sent_at"] > ACTION_ACK_TIMEOUT_S:
                self._pending.pop(ref, None)
                if not pending["future"].done():
                    pending["future"].set_result(None)
                self.log.warning(
                    "动作未回执（%.1fs 超时）：ref=%s kind=%s",
                    ACTION_ACK_TIMEOUT_S,
                    ref,
                    pending["kind"],
                )

    def _fail_pending(self, reason: str) -> None:
        for ref, pending in self._pending.items():
            if not pending["future"].done():
                pending["future"].set_result(None)
            self.log.warning("在途动作终止（%s）：ref=%s kind=%s", reason, ref, pending["kind"])
        self._pending.clear()

    async def _heartbeat_loop(self) -> None:
        """固定 0.25s 回收超时动作；ping_interval>0 时按间隔发送 ping。"""
        next_ping = (
            time.monotonic() + self._ping_interval if self._ping_interval > 0 else None
        )
        while True:
            await asyncio.sleep(0.25)
            self._reap_pending()
            if next_ping is not None and time.monotonic() >= next_ping:
                client = self._client
                if client is not None and client.ready:
                    await client.send("ping", {})
                next_ping = time.monotonic() + self._ping_interval
