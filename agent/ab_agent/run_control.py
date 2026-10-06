"""对局内动作编排（票据 07/09 的 RUNNING 阶段）。

职责：
- 战斗走位（默认 ``ReflexController``，票据 09；可注入占位控制器做对照）；
- 商店固定动作：打印摘要后直接离开（每店一次，失败冷却重试；经济策略见票据 12）；
- 升级固定动作：选第一张可选卡（每套选项一次，失败冷却重试；评分见票据 12）；
- 战报数据记账：波次/金币/材料/最终构建。

不管理阶段（Phase）与连接：由 ``RunSession`` 在 RUNNING 且已接管时驱动本控制器。
"""
from __future__ import annotations

import asyncio
from enum import Enum
from typing import Callable, Optional

from .decision.reflex import ReflexController
from .move_control import MoveController
from .menu_view import (
    BattleReport,
    RunEndMenu,
    first_pickable_slot,
    format_level_up_summary,
    format_shop_summary,
)
from .protocol import ack_error, ack_ok
from .state import AgentState

#: 固定动作失败后的重试冷却（秒）
ACTION_RETRY_COOLDOWN_S = 1.5


class Activity(str, Enum):
    COMBAT = "combat"
    SHOP = "shop"
    LEVEL_UP = "level_up"


class RunController:
    """RUNNING 阶段动作编排与记账。"""

    def __init__(
        self,
        server,
        *,
        output: Callable[[str], None] = print,
        clock: Callable[[], float],
        autopilot: Optional[MoveController] = None,
        replay_path: Optional[Callable[[], Optional[str]]] = None,
        ack_timeout_s: float,
    ) -> None:
        self.server = server
        self._out = output
        self._clock = clock
        self._autopilot = autopilot or ReflexController()
        self._replay_path = replay_path or (lambda: None)
        self._ack_timeout_s = ack_timeout_s
        self.activity: Optional[Activity] = None
        self.max_wave: Optional[int] = None
        self._last_gold: Optional[int] = None
        self._last_materials: Optional[int] = None
        self._last_inventory: dict = {}
        self._shop_visit_seen = False
        self._shop_pending = False
        self._shop_left = False
        self._shop_retry_at = 0.0
        self._level_up_signature: Optional[tuple] = None
        self._level_up_pending = False
        self._level_up_retry_at = 0.0

    # ---- 生命周期 ----

    def begin(self) -> None:
        """新对局开始：首次进入第 1 波，清空上一局记账。"""
        self.activity = Activity.COMBAT
        self.max_wave = 1
        self._last_gold = None
        self._last_materials = None
        self._last_inventory = {}
        self._autopilot.reset()
        self._reset_shop()
        self._reset_level_up()

    def reset(self) -> None:
        """停止动作（stop/断线/终局）：清空对局内动作状态。"""
        self.activity = None
        self._autopilot.reset()
        self._reset_shop()
        self._reset_level_up()

    def _reset_shop(self) -> None:
        self._shop_visit_seen = False
        self._shop_pending = False
        self._shop_left = False
        self._shop_retry_at = 0.0

    def _reset_level_up(self) -> None:
        self._level_up_signature = None
        self._level_up_pending = False
        self._level_up_retry_at = 0.0

    # ---- 观测记账 ----

    def observe(self, snapshot: dict) -> None:
        """从快照累计战报数据（波次/金币/材料/构建）。"""
        wave = snapshot.get("wave")
        if isinstance(wave, dict):
            index = wave.get("index")
            if isinstance(index, int) and not isinstance(index, bool):
                self.max_wave = index if self.max_wave is None else max(self.max_wave, index)
        economy = snapshot.get("economy")
        if isinstance(economy, dict):
            gold = economy.get("gold")
            if isinstance(gold, int) and not isinstance(gold, bool):
                self._last_gold = gold
            materials = economy.get("materials_this_wave")
            if isinstance(materials, int) and not isinstance(materials, bool):
                self._last_materials = materials
        inventory = snapshot.get("inventory")
        if isinstance(inventory, dict) and (inventory.get("weapons") or inventory.get("items")):
            self._last_inventory = inventory

    # ---- 周期动作 ----

    async def tick(self, state: AgentState, menu: Optional[dict], now: float) -> None:
        """一轮对局动作：升级页 > 商店 > 战斗（互斥）。"""
        if menu is not None and menu.get("phase") == "level_up":
            self.activity = Activity.LEVEL_UP
            await self._handle_level_up(menu, now)
            return
        shop = state.fresh_shop(now=now)
        if shop is not None and not _snapshot_combat(state, now):
            self.activity = Activity.SHOP
            await self._handle_shop(shop, now)
            return
        self._shop_visit_seen = False
        self._shop_left = False
        self.activity = Activity.COMBAT
        snapshot = state.fresh_snapshot(now=now)
        if snapshot is None:
            return
        vector = self._autopilot.next_move(snapshot, now)
        if vector is not None:
            await self.server.send_action("move", {"vector": vector})

    async def _handle_shop(self, shop: dict, now: float) -> None:
        if not self._shop_visit_seen:
            self._shop_visit_seen = True
            self._shop_pending = False
            self._print(format_shop_summary(shop))
        if self._shop_left or self._shop_pending or now < self._shop_retry_at:
            return
        if shop.get("can_leave") is not True:
            return
        self._shop_pending = True
        asyncio.create_task(self._send_shop_leave())

    async def _send_shop_leave(self) -> None:
        ref = await self.server.send_action("shop_leave", {}, track_ack=True)
        if ref is None:
            self._shop_pending = False
            return
        ack = await self.server.wait_ack(ref, timeout=self._ack_timeout_s)
        self._shop_pending = False
        if ack_ok(ack):
            self._shop_left = True
            return
        self._shop_retry_at = self._clock() + ACTION_RETRY_COOLDOWN_S
        self._print(
            "[商店] 离开失败：%s（%.1fs 后重试）" % (ack_error(ack), ACTION_RETRY_COOLDOWN_S)
        )

    async def _handle_level_up(self, menu: dict, now: float) -> None:
        signature = _level_up_signature(menu)
        if signature != self._level_up_signature:
            self._level_up_signature = signature
            self._level_up_pending = False
            self._level_up_retry_at = 0.0
            self._print(format_level_up_summary(menu))
        if self._level_up_pending or now < self._level_up_retry_at:
            return
        slot = first_pickable_slot(menu)
        if slot is None:
            return
        self._level_up_pending = True
        asyncio.create_task(self._pick_upgrade(slot))

    async def _pick_upgrade(self, slot: int) -> None:
        ref = await self.server.send_action(
            "menu_pick_upgrade", {"index": slot}, track_ack=True
        )
        if ref is None:
            self._level_up_pending = False
            return
        ack = await self.server.wait_ack(ref, timeout=self._ack_timeout_s)
        self._level_up_pending = False
        self._level_up_retry_at = self._clock() + ACTION_RETRY_COOLDOWN_S
        if ack_ok(ack):
            self._print("[升级] 已选择第 %d 项" % slot)
            return
        self._print(
            "[升级] 选卡失败：%s（%.1fs 后重试）" % (ack_error(ack), ACTION_RETRY_COOLDOWN_S)
        )

    # ---- 战报与视图 ----

    def build_report(
        self,
        run_end: Optional[RunEndMenu],
        *,
        duration_s: Optional[float],
        note: Optional[str] = None,
    ) -> BattleReport:
        if run_end is not None:
            result = run_end.result
            wave = run_end.wave if run_end.wave is not None else self.max_wave
            stats = run_end.stats
        else:
            result = "defeat"
            wave = self.max_wave
            stats = {}
        inventory = self._last_inventory if isinstance(self._last_inventory, dict) else {}
        return BattleReport(
            result=result,
            wave=wave,
            duration_s=duration_s,
            gold=self._last_gold,
            materials=self._last_materials,
            weapons=tuple(inventory.get("weapons") or []),
            items=tuple(inventory.get("items") or []),
            stats=stats,
            replay_path=self._replay_path(),
            note=note,
        )

    def describe(self) -> str:
        if self.activity is None:
            if self.max_wave is not None:
                return "第 %d 波" % self.max_wave
            return "对局中"
        if self.max_wave is None:
            return self.activity.value
        return "%s 第 %d 波" % (self.activity.value, self.max_wave)

    def _print(self, text: str) -> None:
        self._out(text)


def _snapshot_combat(state: AgentState, now: float) -> bool:
    snapshot = state.fresh_snapshot(now=now)
    if snapshot is None:
        return False
    wave = snapshot.get("wave")
    return (
        isinstance(wave, dict)
        and wave.get("phase") == "combat"
        and snapshot.get("player") is not None
    )


def _level_up_signature(menu: dict) -> tuple:
    return tuple(
        (
            option.get("slot"),
            option.get("kind"),
            option.get("id"),
            option.get("tier"),
            option.get("can_pick"),
        )
        for option in menu.get("options") or []
        if isinstance(option, dict)
    )
