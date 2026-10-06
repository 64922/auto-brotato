"""agent 观测状态：最新快照/商店视图、链路健康与 OBSERVE_ONLY 标志。

单线程（asyncio 事件循环）内使用；本模块不做 IO、不做决策。状态机（票据 07）
在此之上构建。
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Optional

#: 计算链路频率的滑动窗口（60Hz 下约 2s）
SNAPSHOT_HZ_WINDOW = 120

#: ready 后超过该秒数未收到快照视为链路不健康（protocol.md §1 兜底 2s）
LINK_TIMEOUT_S = 2.0


class AgentState:
    """最新观测与链路状态（视图模型）。"""

    def __init__(self) -> None:
        self.connected = False
        self.session_id: Optional[str] = None
        self.hello: Optional[dict] = None
        self.observe_only = True
        self.latest_snapshot: Optional[dict] = None
        self.latest_snapshot_at: Optional[float] = None
        self.latest_shop: Optional[dict] = None
        self.latest_shop_at: Optional[float] = None
        self.last_ack: Optional[dict] = None
        self.last_ack_ref: Any = None
        self.last_event: Optional[dict] = None
        self.last_pong_at: Optional[float] = None
        self.snapshot_count = 0
        self.shop_count = 0
        self.protocol_errors = 0
        self._snapshot_times: Deque[float] = deque(maxlen=SNAPSHOT_HZ_WINDOW)

    # ---- 连接生命周期 ----

    def mark_connected(self, session_id: str | None, hello: dict) -> None:
        """握手完成；重连后默认 OBSERVE_ONLY（架构 §3.1 不变量 4）。"""
        self.connected = True
        self.session_id = session_id
        self.hello = hello
        self.observe_only = True
        self._snapshot_times.clear()

    def mark_disconnected(self) -> None:
        self.connected = False

    # ---- 观测更新 ----

    def update_snapshot(self, payload: dict, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.latest_snapshot = payload
        self.latest_snapshot_at = now
        self.snapshot_count += 1
        self._snapshot_times.append(now)

    def update_shop(self, payload: dict, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.latest_shop = payload
        self.latest_shop_at = now
        self.shop_count += 1

    def note_ack(self, ref: Any, payload: dict) -> None:
        self.last_ack = payload
        self.last_ack_ref = ref

    def note_event(self, payload: dict) -> None:
        self.last_event = payload

    def note_pong(self, *, now: float | None = None) -> None:
        self.last_pong_at = time.monotonic() if now is None else now

    def note_protocol_error(self) -> None:
        self.protocol_errors += 1

    # ---- 派生视图 ----

    def snapshot_hz(self) -> float:
        """按接收时间估算快照频率；样本不足返回 0.0。"""
        if len(self._snapshot_times) < 2:
            return 0.0
        span = self._snapshot_times[-1] - self._snapshot_times[0]
        if span <= 0:
            return 0.0
        return (len(self._snapshot_times) - 1) / span

    def snapshot_age(self, *, now: float | None = None) -> Optional[float]:
        """距最近一条快照的秒数；从未收到返回 None。"""
        if self.latest_snapshot_at is None:
            return None
        now = time.monotonic() if now is None else now
        return now - self.latest_snapshot_at

    def link_healthy(self, *, now: float | None = None, timeout: float = LINK_TIMEOUT_S) -> bool:
        age = self.snapshot_age(now=now)
        return self.connected and age is not None and age <= timeout

    def wave_summary(self) -> str:
        """最新快照的波次/玩家摘要（中文一行）。"""
        snapshot = self.latest_snapshot
        if snapshot is None:
            return "无快照"
        wave = snapshot.get("wave")
        player = snapshot.get("player") or {}
        parts = []
        if isinstance(wave, dict):
            index = wave.get("index")
            phase = wave.get("phase")
            time_left = wave.get("time_left")
            parts.append("第 %s 波" % index)
            if phase is not None:
                parts.append(str(phase))
            if isinstance(time_left, (int, float)):
                parts.append("剩余 %.0fs" % time_left)
        else:
            parts.append("未在对局（wave=null）")
        if player:
            alive = player.get("alive")
            hp = player.get("hp")
            parts.append("玩家%s" % ("存活" if alive else "阵亡"))
            if isinstance(hp, (int, float)):
                parts.append("hp=%.0f" % hp)
        return " · ".join(parts)

    def describe(self, *, now: float | None = None) -> str:
        """一行状态摘要（CLI status / 周期打印）。"""
        if self.connected:
            head = "连接=已连接（session=%s）" % (self.session_id or "?")
            health = "链路=%s" % ("健康" if self.link_healthy(now=now) else "不健康")
        else:
            head = "连接=未连接"
            health = None
        parts = [head, "模式=%s" % ("OBSERVE_ONLY" if self.observe_only else "接管")]
        if health is not None:
            parts.append(health)
        if self.latest_snapshot_at is None:
            parts.append("快照=无")
        else:
            age = self.snapshot_age(now=now)
            parts.append(
                "快照=%.1fHz（共 %d 条，最近 %.2fs 前）"
                % (self.snapshot_hz(), self.snapshot_count, age)
            )
        wave = self.wave_summary()
        if not self.connected and self.latest_snapshot is not None:
            wave = "上次快照：" + wave
        parts.append(wave)
        return " · ".join(parts)
