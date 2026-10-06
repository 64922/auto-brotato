"""经济层动作执行：串行下发 ``shop_*`` / ``menu_pick_upgrade`` 并处理 ack 与重试（票据 12）。

- **串行**：一次只发一个动作，等待回执后才推进（mod 侧队列本身也串行，这里保证 agent
  不会因 60Hz tick 重复发动作造成风暴）；
- **失败重试按错误码区分**（工单备注）：
  - ``insufficient_gold``：不重试，本次进店抑制该动作；
  - 超时类（``buy_timeout``/``sell_failed``/``reroll_failed``/``lock_failed``/``pick_timeout``/
    ``no_ack``）：重试一次，仍失败则抑制；``shop_leave`` 例外——没有替代动作，冷却后持续
    重试（抑制会卡死在商店）；
  - ``busy``/``cannot_leave``/``shop_closed``：冷却后重试（不计数）；
  - ``bad_*``/``slot_sold``/``not_lockable`` 等：不重试，抑制该动作或等待新视图；
  - ``not_in_shop``/``not_in_level_up``：页面已切换，视为该次访问结束；
- **旧视图冻结**：任一商店动作成功后，同指纹的视图被冻结（心跳窗口 ≤1s），避免基于旧
  金币/库存重复购买、级联误卖或重复锁定；
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
    {"buy_timeout", "sell_failed", "reroll_failed", "lock_failed", "pick_timeout", "no_ack"}
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
        # 成功动作后视图尚未更新时冻结决策（数据变化推送 + 1s 心跳内必达）
        self._shop_stale_signature: Optional[tuple] = None
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
        self._shop_stale_signature = None
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
        signature = _shop_signature(shop)
        if key != self._shop_key:
            self._begin_shop_visit(key)
        if self._shop_stale_signature is not None:
            if signature == self._shop_stale_signature:
                return  # 动作已成功但视图未更新：等待推送（心跳 ≤1s），避免基于旧视图决策
            self._shop_stale_signature = None
        if self._pending or self._shop_done or now < self._shop_retry_at:
            return
        plan = self.planner.plan_shop(
            shop,
            context,
            rerolls_used=self._shop_rerolls,
            blocked=self._shop_blocked,
        )
        self._pending = True
        asyncio.create_task(self._execute_shop(server, plan, key, signature))

    def _begin_shop_visit(self, key: Optional[int]) -> None:
        self._shop_key = key
        self._shop_blocked = set()
        self._shop_retried = set()
        self._shop_rerolls = 0
        self._shop_done = False
        self._shop_retry_at = 0.0
        self._shop_stale_signature = None

    async def _execute_shop(
        self, server, plan: ShopPlan, visit_key: Optional[int], signature: tuple
    ) -> None:
        try:
            self._record(server, plan.action, plan.params, plan.reason)
            ref = await server.send_action(plan.action, dict(plan.params), track_ack=True)
            if ref is None:
                return
            ack = await server.wait_ack(ref, timeout=self._ack_timeout_s)
            if visit_key != self._shop_key:
                return  # 商店访问已切换（新波次），旧动作结果不污染新状态
            if ack is not None and ack.get("ok") is True:
                self._on_shop_success(plan, signature)
                return
            error = _error_of(ack)
            self._on_shop_failure(plan, error)
        except Exception as exc:  # noqa: BLE001 - 兜底：任务异常不得卡死经济层
            self._out("[经济] %s 执行异常：%r" % (plan.action, exc))
        finally:
            self._pending = False

    def _on_shop_success(self, plan: ShopPlan, signature: tuple) -> None:
        if plan.action == ACTION_LEAVE:
            self._shop_done = True
            return
        # 买入/卖出/刷新/锁定都会改变商店数据：等新视图到达再决策（防止旧视图重复
        # 购买、级联误卖、重复锁定）；槽位键同时抑制，兜底迟到的旧签名更新。
        self._shop_stale_signature = signature
        if plan.action == ACTION_BUY:
            self._shop_blocked.add(plan.target_key)
        elif plan.action == ACTION_REROLL:
            self._shop_rerolls += 1
        elif plan.action == ACTION_LOCK:
            self._shop_blocked.add(plan.target_key)

    def _on_shop_failure(self, plan: ShopPlan, error: str) -> None:
        key = plan.target_key or plan.action
        if plan.action == ACTION_LEAVE and error in TIMEOUT_ERRORS:
            # 离开没有替代动作：抑制会导致卡死在商店，只能冷却后继续重试
            self._shop_retry_at = self._clock() + RETRY_COOLDOWN_S
            self._out("[经济] 离开商店未成功：%s（%.1fs 后重试）" % (error, RETRY_COOLDOWN_S))
            return
        if error == "insufficient_gold":
            self._shop_blocked.add(key)
            self._out("[经济] %s 金币不足，本次访问不再尝试" % plan.action)
        elif error in TIMEOUT_ERRORS:
            if key in self._shop_retried:
                self._shop_blocked.add(key)
                self._out("[经济] %s 再次超时（%s），本次访问放弃该动作" % (plan.action, error))
            else:
                self._shop_retried.add(key)
                self._out(
                    "[经济] %s 超时（%s），%.1fs 后重试一次"
                    % (plan.action, error, RETRY_COOLDOWN_S)
                )
        elif error in TRANSIENT_ERRORS:
            self._out(
                "[经济] %s 暂时失败（%s），%.1fs 后重试"
                % (plan.action, error, RETRY_COOLDOWN_S)
            )
        elif error in PAGE_GONE_ERRORS:
            self._shop_done = True
        else:
            self._shop_blocked.add(key)
            self._out("[经济] %s 失败：%s（不再重试）" % (plan.action, error))
        self._shop_retry_at = self._clock() + RETRY_COOLDOWN_S

    # ---- 升级选卡 ----

    async def handle_level_up(
        self,
        server,
        menu: Mapping[str, Any],
        context: EconomyContext,
        now: float,
    ) -> None:
        """tick 驱动：升级页出现时评分选卡（同组选项成功后不重复选）。"""
        signature = options_signature(menu)
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
        try:
            self._record(server, ACTION_PICK, {"index": plan.slot}, plan.reason)
            ref = await server.send_action(
                ACTION_PICK, {"index": plan.slot}, track_ack=True
            )
            if ref is None:
                return
            ack = await server.wait_ack(ref, timeout=self._ack_timeout_s)
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
                self._out(
                    "[经济] 选卡暂时失败（%s），%.1fs 后重试" % (error, RETRY_COOLDOWN_S)
                )
            elif error in TIMEOUT_ERRORS:
                if key in self._level_retried:
                    self._level_picked = signature
                    self._out("[经济] 选卡再次超时（%s），本组放弃" % error)
                else:
                    self._level_retried.add(key)
                    self._out(
                        "[经济] 选卡超时（%s），%.1fs 后重试一次"
                        % (error, RETRY_COOLDOWN_S)
                    )
            else:
                self._level_blocked.add(key)
                self._out("[经济] 选卡失败：%s（改选其他卡）" % error)
            self._level_retry_at = self._clock() + RETRY_COOLDOWN_S
        except Exception as exc:  # noqa: BLE001 - 兜底：任务异常不得卡死经济层
            self._out("[经济] 选卡执行异常：%r" % exc)
        finally:
            self._pending = False

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


def _shop_signature(shop: Mapping[str, Any]) -> tuple:
    """商店视图的决策相关指纹（金币/刷新/槽位；用于识别推送是否已反映动作）。"""
    reroll = shop.get("reroll") if isinstance(shop.get("reroll"), Mapping) else {}
    slots = tuple(
        (
            raw.get("slot"),
            raw.get("kind"),
            raw.get("id"),
            raw.get("tier"),
            raw.get("price"),
            raw.get("sold"),
            raw.get("locked"),
        )
        for raw in shop.get("slots") or []
        if isinstance(raw, Mapping)
    )
    return (
        shop.get("wave_next"),
        shop.get("gold"),
        reroll.get("cost"),
        reroll.get("count"),
        slots,
    )


def options_signature(menu: Mapping[str, Any]) -> tuple:
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
