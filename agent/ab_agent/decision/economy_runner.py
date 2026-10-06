"""经济层动作执行：串行下发 ``shop_*`` / ``menu_pick_upgrade`` 并处理 ack 与重试（票据 12）。

- **串行**：一次只发一个动作，等待回执后才推进（mod 侧队列本身也串行，这里保证 agent
  不会因 60Hz tick 重复发动作造成风暴）；
- **失败重试按错误码区分**（工单备注）：
  - ``insufficient_gold``：不重试，本次进店抑制该动作；
  - 超时类（``buy_timeout``/``sell_failed``/``reroll_failed``/``lock_failed``/``pick_timeout``）：
    重试一次，仍失败则抑制；
  - ``busy``/``cannot_leave``/``shop_closed``：冷却后重试（不计数）；
  - ``bad_*``/``slot_sold``/``not_lockable`` 等：不重试，抑制该动作或等待新视图；
  - ``not_in_shop``/``not_in_level_up``：页面已切换，视为该次访问结束；
- **决策落盘**：每次下发动作前写 ``{kind:"decision"}`` 行（recorder 接口，票据 03），
  action 为 ``{kind, 参数}``、reason 为评分明细（中文）。

所有状态在 :meth:`reset` 清空（新对局/断线/停止）；商店访问以 ``wave_next`` 为界，
升级页以选项签名（含 ``can_pick``）为界——选卡成功后同签名不再重复选（避免票据 08
记录的心跳窗口内二次选卡 ``not_in_level_up``）。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Mapping, Optional, Sequence

from .economy import (
    ACTION_BUY,
    ACTION_LEAVE,
    ACTION_LOCK,
    ACTION_PICK,
    ACTION_REROLL,
    ACTION_SELL,
    EconomyPlanner,
)
from .economy_model import EconomyContext, ShopPlan, UpgradePlan

#: 动作失败后的重试冷却（秒；避免动作风暴）
RETRY_COOLDOWN_S = 1.0

#: 超时类错误（重试一次）；``no_ack`` 为 agent 侧 2s 未回执
TIMEOUT_ERRORS = frozenset(
    {"buy_timeout", "sell_failed", "reroll_failed", "lock_failed", "no_ack"}
)

#: 可冷却重试的瞬时错误（不计数）
TRANSIENT_ERRORS = frozenset({"busy", "cannot_leave", "shop_closed"})

#: 页面已结束的错误（停止本页动作，等待新视图/新访问）
PAGE_GONE_ERRORS = frozenset({"not_in_shop", "not_in_level_up"})


class EconomyRunner:
    """商店/升级动作的串行执行器（单线程 asyncio 内使用）。"""

    def __init__(
        self,
        planner: EconomyPlanner,
        *,
        output: Callable[[str], None] = print,
        clock: Callable[[], float],
        ack_timeout_s: float,
    ) -> None:
        self.planner = planner
        self._out = output
        self._clock = clock
        self._ack_timeout_s = ack_timeout_s
        self._pending = False
        # 商店访问状态（wave_next 变化即视为新访问）
        self._shop_key: Optional[int] = None
        self._shop_blocked: set[str] = set()
        self._shop_retried: set[str] = set()
        self._shop_rerolls = 0
        self._shop_done = False
        self._shop_retry_at = 0.0
        # 升级页状态（选项签名变化即视为新一组）
        self._level_signature: Optional[tuple] = None
        self._level_picked: Optional[tuple] = None
        self._level_blocked: set[str] = set()
        self._level_retried: set[str] = set()
        self._level_retry_at = 0.0

    # ---- 生命周期 ----

    def reset(self) -> None:
        """新对局/停止/断线：清空全部访问状态。"""
        self._pending = False
        self._shop_key = None
        self._shop_blocked = set()
        self._shop_retried = set()
        self._shop_rerolls = 0
        self._shop_done = False
        self._shop_retry_at = 0.0
        self._level_signature = None
        self._level_picked = None
        self._level_blocked = set()
        self._level_retried = set()
        self._level_retry_at = 0.0

    @property
    def busy(self) -> bool:
        """是否有动作在途（状态/测试可观测）。"""
        return self._pending

    # ---- 商店 ----

    async def handle_shop(
        self,
        server,
        shop: Mapping[str, Any],
        context: EconomyContext,
        now: float,
    ) -> None:
        """tick 驱动：每次最多发起一个商店动作（无动作在途且未结束时）。"""
        key = _wave_next(shop)
        if key != self._shop_key:
            self._begin_shop_visit(key)
        if self._pending or self._shop_done or now < self._shop_retry_at:
            return
        plan = self.planner.plan_shop(
            shop,
            context,
            rerolls_used=self._shop_rerolls,
            blocked=self._shop_blocked,
        )
        self._pending = True
        asyncio.create_task(self._execute_shop(server, plan, key, now))

    def _begin_shop_visit(self, key: Optional[int]) -> None:
        self._shop_key = key
        self._shop_blocked = set()
        self._shop_retried = set()
        self._shop_rerolls = 0
        self._shop_done = False
        self._shop_retry_at = 0.0

    async def _execute_shop(
        self, server, plan: ShopPlan, visit_key: Optional[int], now: float
    ) -> None:
        self._record(server, plan.action, plan.params, plan.reason)
        ref = await server.send_action(plan.action, dict(plan.params), track_ack=True)
        if ref is None:
            self._pending = False
            return
        ack = await server.wait_ack(ref, timeout=self._ack_timeout_s)
        self._pending = False
        if visit_key != self._shop_key:
            return  # 商店访问已切换（新波次），旧动作结果不污染新状态
        if ack is not None and ack.get("ok") is True:
            self._on_shop_success(plan)
            return
        error = _error_of(ack)
        self._on_shop_failure(plan, error, visit_key)

    def _on_shop_success(self, plan: ShopPlan) -> None:
        if plan.action == ACTION_BUY:
            # 商店视图推送存在延迟：购买的槽位先抑制，避免旧视图重复购买
            self._shop_blocked.add(plan.target_key)
        elif plan.action == ACTION_REROLL:
            self._shop_rerolls += 1
        elif plan.action == ACTION_LEAVE:
            self._shop_done = True

    def _on_shop_failure(
        self, plan: ShopPlan, error: str, visit_key: Optional[int]
    ) -> None:
        key = plan.target_key or plan.action
        if error == "insufficient_gold":
            self._shop_blocked.add(key)
        elif error in TIMEOUT_ERRORS:
            if key in self._shop_retried:
                self._shop_blocked.add(key)
                self._out("[经济] %s 再次超时（%s），本次访问放弃该动作" % (plan.action, error))
            else:
                self._shop_retried.add(key)
        elif error in TRANSIENT_ERRORS:
            pass  # 仅冷却
        elif error in PAGE_GONE_ERRORS:
            self._shop_done = True
        else:
            self._shop_blocked.add(key)
            self._out("[经济] %s 失败：%s（不再重试）" % (plan.action, error))
        self._shop_retry_at = self._clock() + RETRY_COOLDOWN_S
        if error not in PAGE_GONE_ERRORS and error not in TRANSIENT_ERRORS:
            self._out("[经济] %s 未成功：%s（%.1fs 后重试/改选）" % (plan.action, error, RETRY_COOLDOWN_S))

    # ---- 升级选卡 ----

    async def handle_level_up(
        self,
        server,
        menu: Mapping[str, Any],
        context: EconomyContext,
        now: float,
    ) -> None:
        """tick 驱动：升级页出现时评分选卡（同组选项成功后不重复选）。"""
        signature = _options_signature(menu)
        if signature != self._level_signature:
            self._level_signature = signature
            self._level_blocked = set()
            self._level_retried = set()
            self._level_retry_at = 0.0
        if self._level_picked == signature:
            return  # 本组已成功选卡（页关心跳窗口内不重复选）
        if self._pending or now < self._level_retry_at:
            return
        plan = self.planner.plan_level_up(menu, context, blocked=self._level_blocked)
        if plan.slot is None:
            return
        self._pending = True
        asyncio.create_task(self._execute_level_up(server, plan, signature))

    async def _execute_level_up(
        self, server, plan: UpgradePlan, signature: tuple
    ) -> None:
        self._record(server, ACTION_PICK, {"index": plan.slot}, plan.reason)
        ref = await server.send_action(
            ACTION_PICK, {"index": plan.slot}, track_ack=True
        )
        if ref is None:
            self._pending = False
            return
        ack = await server.wait_ack(ref, timeout=self._ack_timeout_s)
        self._pending = False
        if signature != self._level_signature:
            return  # 选项已更新（新的升级组），旧结果不污染新状态
        if ack is not None and ack.get("ok") is True:
            self._level_picked = signature
            return
        error = _error_of(ack)
        key = plan.target_key
        if error in PAGE_GONE_ERRORS:
            self._level_picked = signature
            return
        if error in TRANSIENT_ERRORS:
            pass  # 仅冷却
        elif error in TIMEOUT_ERRORS or error == "pick_timeout":
            if key in self._level_retried:
                self._level_picked = signature
                self._out("[经济] 选卡再次超时（%s），本组放弃" % error)
            else:
                self._level_retried.add(key)
        else:
            self._level_blocked.add(key)
        self._level_retry_at = self._clock() + RETRY_COOLDOWN_S
        self._out(
            "[经济] 选卡未成功：%s（%.1fs 后重试/改选）"
            % (error or "未回执", RETRY_COOLDOWN_S)
        )

    # ---- 记录 ----

    def _record(
        self, server, action_kind: str, params: Mapping[str, Any], reason: str
    ) -> None:
        """写回放 decision 行并打印中文理由（layer=economy，票据 03 接口）。"""
        action = {"kind": action_kind}
        action.update(params)
        recorder = getattr(server, "recorder", None)
        if recorder is not None:
            recorder.record_decision("economy", action, reason)
        self._out("[经济] %s" % reason)


def _wave_next(shop: Mapping[str, Any]) -> Optional[int]:
    value = shop.get("wave_next")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _options_signature(menu: Mapping[str, Any]) -> tuple:
    options: Sequence[Mapping[str, Any]] = [
        option
        for option in menu.get("options") or []
        if isinstance(option, Mapping)
    ]
    return tuple(
        (
            option.get("slot"),
            option.get("kind"),
            option.get("id"),
            option.get("tier"),
            option.get("can_pick"),
        )
        for option in options
    )


def _error_of(ack: Optional[Mapping[str, Any]]) -> str:
    if not isinstance(ack, Mapping):
        return "no_ack"
    error = ack.get("error")
    return str(error) if error else "unknown_error"
