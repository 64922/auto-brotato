"""经济层决策：商店规则（购买/刷新/锁定/出售/离开）与升级选卡（strategy.md §5，票据 12）。

本模块是**纯决策**（无 IO、无随机、无时间依赖）：给定观测视图 + 参数 + 知识库，输出
建议动作与中文理由。单件评分口径见 :mod:`ab_agent.decision.economy_scoring`；动作下发
（``shop_*`` / ``menu_pick_upgrade``）、ack 等待与失败重试由
:mod:`ab_agent.decision.economy_runner` 负责；对局内接线见 ``run_control.py``。

规则摘要：

- 购买：``score >= buy_threshold`` 且扣价后仍高于波次安全余量；
- 合并：背包已有同族同阶武器时优先（Brotato 同 id 同级两把自动合并升级）；
- 出售：仅"武器位满且低价值"或"差金币买关键物"时卖武器（道具不可卖）；
- 刷新：仅当在售槽位全部低价值且刷新预算（随波次增长）未耗尽；
- 锁定：高价值但买不起 → 锁定跨波保留（``lock_enabled``）；
- 离开：无高价值可买或预算耗尽。

知识库缺失（票据 11 加载失败）时降级：商店直接离开、升级选第一张可选卡——保证流程
不中断（票据 11 的"加载失败不阻断启动"约定）。
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from ..knowledge import KnowledgeBase
from .economy_config import EconomyConfig, HeroProfile, load_economy_config
from .economy_model import (
    Appraisal,
    EconomyContext,
    ShopPlan,
    UpgradePlan,
    as_int,
    items,
)
from .economy_scoring import OfferScorer

#: 商店动作（协议 §5.1）
ACTION_BUY = "shop_buy"
ACTION_SELL = "shop_sell"
ACTION_REROLL = "shop_reroll"
ACTION_LOCK = "shop_lock"
ACTION_LEAVE = "shop_leave"
ACTION_PICK = "menu_pick_upgrade"


class EconomyPlanner:
    """商店/升级的纯决策器（无状态；同样的输入总是同样的输出）。"""

    def __init__(
        self,
        knowledge: Optional[KnowledgeBase] = None,
        config: Optional[EconomyConfig] = None,
    ) -> None:
        self.knowledge = knowledge
        self.config = config or load_economy_config()
        self.scorer = OfferScorer(knowledge, self.config)

    # ---- 对外接口 ----

    def plan_shop(
        self,
        shop: Mapping[str, Any],
        context: EconomyContext,
        *,
        rerolls_used: int = 0,
        blocked: Sequence[str] = (),
    ) -> ShopPlan:
        """评估商店并给出下一步动作（buy/sell/reroll/lock/leave）。

        ``blocked`` 为本次进店已被否决的动作键（如 ``shop_buy:2``），失败重试规则由
        runner 维护；``rerolls_used`` 为本店已刷新次数（预算曲线约束）。
        """
        blocked_set = set(blocked)
        if self.knowledge is None:
            return ShopPlan(
                action=ACTION_LEAVE,
                params={},
                reason="无知识库，直接离开商店（降级模式）",
            )
        profile = self.profile(context.hero_id, context.inventory)
        appraisals = self.appraise_shop(shop, context, profile)
        bank = self.config.shop
        gold = as_int(shop.get("gold")) or 0
        margin = self._safety_margin(self.scorer.stage(context.wave))

        best = _best_buy(appraisals, blocked_set, bank.buy_threshold)
        if best is not None:
            if gold - best.price >= margin:
                if self._needs_weapon_slot(best, context):
                    sell = self._pick_sell_candidate(context, profile, reason="腾武器位")
                    if sell is not None:
                        index, sold = sell
                        return ShopPlan(
                            action=ACTION_SELL,
                            params={"inv_kind": "weapon", "index": index},
                            reason="武器位已满：先出售低价值武器 %s（%s），再购买 %s"
                            % (sold.id, sold.score.format(), best.label),
                            target_key="%s:%d" % (ACTION_SELL, index),
                            appraisals=appraisals,
                        )
                    # 无法腾位（全部武器都值得保留）：本件买不了，走后续刷新/离开
                else:
                    return ShopPlan(
                        action=ACTION_BUY,
                        params={"slot": best.slot},
                        reason="购买槽位 %d %s（%s；金币 %d，安全余量 %d）"
                        % (best.slot, best.label, best.score.format(), gold, margin),
                        target_key="%s:%d" % (ACTION_BUY, best.slot),
                        appraisals=appraisals,
                    )
            else:
                sell = self._pick_sell_candidate(context, profile, reason="换取金币")
                if sell is not None:
                    index, sold = sell
                    return ShopPlan(
                        action=ACTION_SELL,
                        params={"inv_kind": "weapon", "index": index},
                        reason="金币不足购买 %s：先出售低价值武器 %s（%s）"
                        % (best.label, sold.id, sold.score.format()),
                        target_key="%s:%d" % (ACTION_SELL, index),
                        appraisals=appraisals,
                    )
                lock = self._lock_plan(best, gold, blocked_set, appraisals)
                if lock is not None:
                    return lock

        # 买动作被失败抑制时，高价值商品仍应锁定跨波保留（不重试购买）
        best_any = _best_buy(appraisals, set(), bank.buy_threshold)
        if (
            best_any is not None
            and gold - best_any.price < margin
            and self._weapon_fits_or_merge(best_any, context)
        ):
            lock = self._lock_plan(best_any, gold, blocked_set, appraisals)
            if lock is not None:
                return lock

        reroll = self._reroll_plan(shop, appraisals, context, rerolls_used, blocked_set)
        if reroll is not None:
            return reroll
        return ShopPlan(
            action=ACTION_LEAVE,
            params={},
            reason="无高价值可买或预算耗尽，离开商店（金币 %d）" % gold,
            appraisals=appraisals,
        )

    def plan_level_up(
        self,
        menu: Mapping[str, Any],
        context: EconomyContext,
        *,
        blocked: Sequence[str] = (),
    ) -> UpgradePlan:
        """升级选卡：按同一价值函数（无价格项）+ 最缺属性短板评分。

        ``blocked``（如 ``menu_pick_upgrade:2``）用于失败后改选其他卡。
        """
        blocked_set = set(blocked)
        options = [
            option
            for option in menu.get("options") or []
            if isinstance(option, Mapping)
            and option.get("can_pick") is True
            and "%s:%s" % (ACTION_PICK, as_int(option.get("slot"))) not in blocked_set
        ]
        if not options:
            return UpgradePlan(slot=None, params={}, reason="升级页没有可选卡")
        if self.knowledge is None:
            slot = as_int(options[0].get("slot"))
            return UpgradePlan(
                slot=slot,
                params={"index": slot},
                reason="无知识库，选择第一张可选卡（降级模式）",
            )
        profile = self.profile(context.hero_id, context.inventory)
        full = self.scorer.weapon_count(context.inventory) >= self.config.shop.max_weapon_slots
        appraisals = []
        for option in options:
            slot = as_int(option.get("slot"))
            if slot is None:
                continue
            kind = str(option.get("kind") or "")
            entry_id = str(option.get("id") or "")
            tier = as_int(option.get("tier")) or 0
            score = self.scorer.score_offer(
                kind,
                entry_id,
                tier,
                price=0,
                context=context,
                profile=profile,
                full_penalty=full,
                use_need=True,
            )
            appraisals.append(
                Appraisal(
                    slot=slot,
                    kind=kind,
                    id=entry_id,
                    tier=tier,
                    price=0,
                    score=score,
                    known=self.scorer.lookup(kind, entry_id) is not None,
                )
            )
        if not appraisals:
            return UpgradePlan(slot=None, params={}, reason="升级页选项缺少槽位信息")
        best = min(appraisals, key=lambda item: (-item.score.total, item.slot))
        reason = "选择槽位 %d %s（%s）" % (best.slot, best.label, best.score.format())
        weakest = self.scorer.weakest_stat(context)
        if best.score.need and weakest:
            reason = reason[:-1] + "；最缺 %s）" % weakest
        return UpgradePlan(
            slot=best.slot,
            params={"index": best.slot},
            reason=reason,
            target_key="%s:%d" % (ACTION_PICK, best.slot),
            appraisals=tuple(appraisals),
        )

    def appraise_shop(
        self,
        shop: Mapping[str, Any],
        context: EconomyContext,
        profile: Optional[HeroProfile] = None,
    ) -> tuple[Appraisal, ...]:
        """对商店全部在售槽位评分（按槽位排序，确定性）。"""
        profile = profile or self.profile(context.hero_id, context.inventory)
        full = self.scorer.weapon_count(context.inventory) >= self.config.shop.max_weapon_slots
        appraisals = []
        for raw in items(shop.get("slots")):
            if raw.get("sold") is True:
                continue
            slot = as_int(raw.get("slot"))
            if slot is None:
                continue
            kind = str(raw.get("kind") or "item")
            entry_id = str(raw.get("id") or "")
            tier = as_int(raw.get("tier")) or 0
            price = as_int(raw.get("price")) or 0
            score = self.scorer.score_offer(
                kind if kind in ("item", "weapon") else "unknown",
                entry_id,
                tier,
                price=price,
                context=context,
                profile=profile,
                full_penalty=full,
            )
            appraisals.append(
                Appraisal(
                    slot=slot,
                    kind=kind,
                    id=entry_id,
                    tier=tier,
                    price=price,
                    score=score,
                    known=self.scorer.lookup(kind, entry_id) is not None,
                )
            )
        appraisals.sort(key=lambda item: item.slot)
        return tuple(appraisals)

    def profile(self, hero_id: str, inventory: Mapping[str, Any]) -> HeroProfile:
        return self.scorer.profile_weights(hero_id, inventory)

    def inventory_strength(
        self, inventory: Mapping[str, Any], context: EconomyContext
    ) -> float:
        return self.scorer.inventory_strength(inventory, context)

    # ---- 规则辅助 ----

    def _weapon_fits_or_merge(self, best: Appraisal, context: EconomyContext) -> bool:
        """商品是否可直接容纳（非武器/未满/合并）——锁定判定用。"""
        if best.kind != "weapon":
            return True
        if self.scorer.weapon_count(context.inventory) < self.config.shop.max_weapon_slots:
            return True
        return "合并" in best.score.flags

    def _needs_weapon_slot(self, best: Appraisal, context: EconomyContext) -> bool:
        return best.kind == "weapon" and not self._weapon_fits_or_merge(best, context)

    def _lock_plan(
        self,
        best: Appraisal,
        gold: int,
        blocked: set[str],
        appraisals: Sequence[Appraisal],
    ) -> Optional[ShopPlan]:
        bank = self.config.shop
        key = "%s:%d" % (ACTION_LOCK, best.slot)
        if not bank.lock_enabled or key in blocked:
            return None
        if best.score.total < bank.lock_min_value or gold >= best.price:
            return None
        return ShopPlan(
            action=ACTION_LOCK,
            params={"slot": best.slot, "locked": True},
            reason="发现高价值但买不起：锁定槽位 %d %s（%s）跨波保留"
            % (best.slot, best.label, best.score.format()),
            target_key=key,
            appraisals=tuple(appraisals),
        )

    def _pick_sell_candidate(
        self, context: EconomyContext, profile: HeroProfile, *, reason: str
    ) -> Optional[tuple[int, Appraisal]]:
        """挑选可出售的最低价值武器（保留 sell_min_weapons 把；只卖低于阈值的）。

        道具不可卖（协议 §5.1），故候选仅武器；``reason="腾武器位"`` 时同样只卖
        低价值武器（高价值武器宁可放弃本次购买）。
        """
        shop = self.config.shop
        weapons = items(context.inventory.get("weapons"))
        if len(weapons) <= shop.sell_min_weapons:
            return None
        candidates = []
        for weapon in weapons:
            index = as_int(weapon.get("slot"))
            if index is None:
                continue
            family = self.scorer.family(str(weapon.get("id") or ""))
            score = self.scorer.weapon_intrinsic(
                family, as_int(weapon.get("tier")) or 0, profile
            )
            candidates.append(
                (
                    score.total,
                    index,
                    Appraisal(
                        slot=index,
                        kind="weapon",
                        id=str(weapon.get("id") or ""),
                        tier=as_int(weapon.get("tier")) or 0,
                        price=0,
                        score=score,
                        known=self.scorer.lookup("weapon", family) is not None,
                    ),
                )
            )
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        score_total, index, appraisal = candidates[0]
        if score_total >= shop.sell_below_score:
            return None
        return index, appraisal

    def _reroll_plan(
        self,
        shop: Mapping[str, Any],
        appraisals: Sequence[Appraisal],
        context: EconomyContext,
        rerolls_used: int,
        blocked: set[str],
    ) -> Optional[ShopPlan]:
        """刷新：仅当现有槽位全部低价值且预算允许（预算按波次增长曲线）。"""
        shop_cfg = self.config.shop
        if ACTION_REROLL in blocked:
            return None
        if any(item.score.total >= shop_cfg.reroll_threshold for item in appraisals):
            return None
        budget = shop_cfg.reroll_budget_base + shop_cfg.reroll_budget_per_wave * max(
            context.wave - 1, 0
        )
        if rerolls_used >= budget:
            return None
        reroll = shop.get("reroll")
        cost = as_int(reroll.get("cost")) if isinstance(reroll, Mapping) else None
        gold = as_int(shop.get("gold")) or 0
        if cost is None or cost < 0 or gold < cost:
            return None
        return ShopPlan(
            action=ACTION_REROLL,
            params={},
            reason="现有槽位全部低价值（最高 %.1f）：刷新一次（费用 %d，预算 %d/%d）"
            % (
                max((item.score.total for item in appraisals), default=0.0),
                cost,
                rerolls_used + 1,
                int(budget),
            ),
            target_key=ACTION_REROLL,
            appraisals=tuple(appraisals),
        )

    def _safety_margin(self, stage: str) -> int:
        shop = self.config.shop
        return int(
            {
                "early": shop.safety_margin_early,
                "mid": shop.safety_margin_mid,
                "late": shop.safety_margin_late,
            }[stage]
        )


def _best_buy(
    appraisals: Sequence[Appraisal], blocked: set[str], threshold: float
) -> Optional[Appraisal]:
    """评分最高且过购买阈值、未被抑制的商品（同分取小槽位）。"""
    candidates = [
        item
        for item in appraisals
        if item.score.total >= threshold
        and "%s:%d" % (ACTION_BUY, item.slot) not in blocked
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda item: (-item.score.total, item.slot))
