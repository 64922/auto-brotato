"""经济层评分原语（strategy.md §5.1 的价值函数，票据 12）。

:class:`OfferScorer` 只做"单件商品/卡片值多少分"的计算：属性权重 × 增量、特殊效果
规则表、Tier 标注、数量协同（同 id 合并/套装跨档）、价格机会成本、升级短板加成，以及
回放回归用的最终构建强度。动作规则（何时买/卖/刷新/锁定/离开）见 ``economy.py``。

纯函数式：无 IO、无随机、无时间依赖；同样输入总是同样输出（ADR-0008）。
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from ..knowledge import KnowledgeBase
from .economy_config import EconomyConfig, HeroProfile
from .economy_model import (
    EconomyContext,
    ScoreBreakdown,
    as_int,
    current_stats,
    items,
)

_STAT_PREFIX = "stat_"


class OfferScorer:
    """基于知识库与配置的单件评分器（无状态）。"""

    def __init__(self, knowledge: Optional[KnowledgeBase], config: EconomyConfig) -> None:
        self.knowledge = knowledge
        self.config = config

    # ---- 单件评分 ----

    def score_offer(
        self,
        kind: str,
        entry_id: str,
        tier: int,
        *,
        price: int,
        context: EconomyContext,
        profile: HeroProfile,
        full_penalty: bool = False,
        use_need: bool = False,
    ) -> ScoreBreakdown:
        """单件商品/卡片的评分明细（kind: item/weapon/upgrade/其他→未知）。"""
        entry = self.lookup(kind, entry_id)
        stage = self.stage(context.wave)
        price_cost = -float(price) * self.gold_cost(stage)
        if entry is None:
            unknown = self.config.upgrade.unknown_score if kind == "upgrade" else 0.0
            return ScoreBreakdown(stat_value=unknown, price=price_cost, flags=("未知条目",))
        stat_value = 0.0
        synergy = 0.0
        flags: list[str] = []
        if kind == "weapon":
            stat_value += self.weapon_dps(entry)
            merge = self.merge_flag(entry_id, tier, context.inventory)
            if merge:
                synergy += self.config.scoring.merge_bonus
                flags.append("合并")
            synergy += self.set_synergy(entry, context.inventory, profile)
            if full_penalty and not merge:
                synergy -= self.config.scoring.weapon_full_penalty
                flags.append("武器位满")
        stat_value += self.value_effects(entry.get("stat_deltas"), profile)
        tier_value = self.tier_value(entry)
        if tier_value:
            flags.append("Tier %s" % self.tier_grade(entry))
        need = self.need_value(entry, context, profile) if use_need else 0.0
        return ScoreBreakdown(
            stat_value=stat_value,
            tier=tier_value,
            synergy=synergy,
            need=need,
            price=price_cost,
            flags=tuple(flags),
        )

    def inventory_strength(
        self, inventory: Mapping[str, Any], context: EconomyContext
    ) -> float:
        """最终构建强度（回放回归指标）：武器 DPS/属性 + 道具属性 + Tier + 套装总量。"""
        if self.knowledge is None:
            return 0.0
        profile = self.profile_weights(context.hero_id, inventory)
        total = 0.0
        for weapon in items(inventory.get("weapons")):
            family = self.family(str(weapon.get("id") or ""))
            tier = as_int(weapon.get("tier")) or 0
            entry = self.weapon_entry(family, tier)
            if entry is None:
                continue
            total += self.weapon_dps(entry)
            total += self.value_effects(entry.get("stat_deltas"), profile)
            total += self.tier_value(entry)
        for item in items(inventory.get("items")):
            entry = self.knowledge.items.get(str(item.get("id") or ""))
            if entry is None:
                continue
            total += self.value_effects(entry.get("stat_deltas"), profile)
            total += self.tier_value(entry)
        total += self.set_total(inventory, profile)
        return total

    # ---- 知识库查找 ----

    def weapon_intrinsic(
        self, family: str, tier: int, profile: HeroProfile
    ) -> ScoreBreakdown:
        """背包内武器自身的固有价值（DPS + 属性 + Tier；不含购买协同/价格）。

        用于出售取舍：合并/套装权益来自"购买动作"，不能算到存货估值上，否则同族
        武器会互相抬价导致永远卖不出去。
        """
        entry = self.weapon_entry(family, tier)
        if entry is None:
            return ScoreBreakdown(flags=("未知条目",))
        return ScoreBreakdown(
            stat_value=self.weapon_dps(entry)
            + self.value_effects(entry.get("stat_deltas"), profile),
            tier=self.tier_value(entry),
        )

    def lookup(self, kind: str, entry_id: str) -> Optional[Mapping[str, Any]]:
        """按类别与 id 查条目；武器 id 支持"族 id"（``weapon_smg`` → ``weapon_smg_1``）。"""
        if self.knowledge is None or not entry_id:
            return None
        if kind == "weapon":
            entry = self.knowledge.weapons.get(entry_id)
            if entry is not None:
                return entry
            family = self.family(entry_id)
            for candidate in self.knowledge.weapons.values():
                if candidate.get("weapon_id") == family:
                    return candidate
            return None
        table = self.knowledge.items if kind == "item" else (
            self.knowledge.upgrades if kind == "upgrade" else None
        )
        return table.get(entry_id) if table is not None else None

    def family(self, weapon_id: str) -> str:
        """武器族 id：``weapon_smg_1`` → ``weapon_smg``；已是族 id 时原样返回。"""
        if self.knowledge is None:
            return weapon_id
        entry = self.knowledge.weapons.get(weapon_id)
        if entry is not None:
            return str(entry.get("weapon_id") or weapon_id)
        return weapon_id

    def weapon_entry(self, family: str, tier: int) -> Optional[Mapping[str, Any]]:
        """按族 id + 阶级查条目（武器合并链上的具体一级）。"""
        if self.knowledge is None:
            return None
        exact = self.knowledge.weapons.get(family)
        if exact is not None and (as_int(exact.get("tier")) or 0) == tier:
            return exact
        for candidate in self.knowledge.weapons.values():
            if candidate.get("weapon_id") == family and (as_int(candidate.get("tier")) or 0) == tier:
                return candidate
        return None

    def weapon_class(self, inventory: Mapping[str, Any]) -> str:
        """背包武器的主流类别（ranged/melee）；读不到返回空串。"""
        if self.knowledge is None:
            return ""
        classes: dict[str, int] = {}
        for weapon in items(inventory.get("weapons")):
            family = self.family(str(weapon.get("id") or ""))
            for entry in self.knowledge.weapons.values():
                if entry.get("weapon_id") == family or entry.get("id") == family:
                    weapon_class = str(entry.get("class") or "")
                    if weapon_class:
                        classes[weapon_class] = classes.get(weapon_class, 0) + 1
                    break
        if not classes:
            return ""
        return max(sorted(classes), key=lambda name: classes[name])

    def weapon_count(self, inventory: Mapping[str, Any]) -> int:
        return len(items(inventory.get("weapons")))

    def profile_weights(self, hero_id: str, inventory: Mapping[str, Any]) -> HeroProfile:
        """构筑权重档案：英雄精确匹配 → 武器类别兜底 → 默认兜底。"""
        assign = self.config.hero_assign
        name = assign.heroes.get(hero_id)
        if name is None:
            name = assign.by_class.get(self.weapon_class(inventory), assign.fallback)
        return self.config.heroes.get(name, self.config.heroes[assign.fallback])

    # ---- 价值分量 ----

    def value_effects(self, deltas: Any, profile: HeroProfile) -> float:
        """属性/效果增量按构筑权重求和；未知键用 ``default_weight``。

        同时接受两种形态：``stat_deltas`` 映射（``键 → 数值``）与套装加成的
        effect 列表（``[{key, value, ...}, ...]``，按 key 累加）。
        """
        entries: list[tuple[str, float]] = []
        if isinstance(deltas, Mapping):
            for key, raw in deltas.items():
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    continue
                entries.append((str(key), float(raw)))
        elif isinstance(deltas, (list, tuple)):
            for effect in deltas:
                if not isinstance(effect, Mapping):
                    continue
                key = effect.get("key")
                raw = effect.get("value")
                if not isinstance(key, str) or not key:
                    continue
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    continue
                entries.append((key, float(raw)))
        scoring = self.config.scoring
        total = 0.0
        for key, value in entries:
            if key.startswith(_STAT_PREFIX):
                weight = profile.weights.get(key[len(_STAT_PREFIX):], scoring.default_weight)
            else:
                weight = scoring.effect_weights.get(key, scoring.default_weight)
            total += weight * value
        return total

    def weapon_dps(self, entry: Mapping[str, Any]) -> float:
        """武器基础输出分：``weapon_dps_weight × damage × 60 / cooldown``（冷却按帧）。"""
        stats = entry.get("stats")
        if not isinstance(stats, Mapping):
            return 0.0
        damage = stats.get("damage")
        cooldown = stats.get("cooldown")
        if (
            isinstance(damage, bool)
            or not isinstance(damage, (int, float))
            or isinstance(cooldown, bool)
            or not isinstance(cooldown, (int, float))
            or cooldown <= 0
        ):
            return 0.0
        return self.config.scoring.weapon_dps_weight * float(damage) * 60.0 / float(cooldown)

    def tier_grade(self, entry: Mapping[str, Any]) -> str:
        rating = self.knowledge.tier_rating(entry) if self.knowledge else None
        return str(rating.get("tier")) if rating else ""

    def tier_value(self, entry: Mapping[str, Any]) -> float:
        grade = self.tier_grade(entry)
        if not grade:
            return 0.0
        return self.config.scoring.tier_bonus.get(grade, 0.0)

    def merge_flag(self, entry_id: str, tier: int, inventory: Mapping[str, Any]) -> bool:
        """背包已有一把同族同阶武器 → 再购一把触发合并升级。"""
        family = self.family(entry_id)
        for weapon in items(inventory.get("weapons")):
            if self.family(str(weapon.get("id") or "")) != family:
                continue
            if (as_int(weapon.get("tier")) or 0) == tier:
                return True
        return False

    def set_synergy(
        self,
        entry: Mapping[str, Any],
        inventory: Mapping[str, Any],
        profile: HeroProfile,
    ) -> float:
        """套装跨档边际价值：从 c 件到 c+1 件新增的加成（bonuses[i] 为 i+1 件总量）。"""
        if self.knowledge is None:
            return 0.0
        sets = entry.get("sets") or []
        if not sets:
            return 0.0
        counts = self.set_counts(inventory)
        total = 0.0
        for set_id in sets:
            bonuses = self.set_bonuses(str(set_id))
            count = counts.get(str(set_id), 0)
            if count >= len(bonuses):
                continue
            marginal = self.value_effects(bonuses[count], profile)
            if count > 0:
                marginal -= self.value_effects(bonuses[count - 1], profile)
            total += marginal * self.config.scoring.set_bonus_scale
        return total

    def set_total(self, inventory: Mapping[str, Any], profile: HeroProfile) -> float:
        """背包当前生效的套装加成总量（构建强度用）。"""
        if self.knowledge is None:
            return 0.0
        total = 0.0
        for set_id, count in self.set_counts(inventory).items():
            bonuses = self.set_bonuses(set_id)
            if 1 <= count <= len(bonuses):
                total += self.value_effects(bonuses[count - 1], profile)
        return total * self.config.scoring.set_bonus_scale

    def set_bonuses(self, set_id: str) -> Sequence[Mapping[str, Any]]:
        entry = self.knowledge.sets.get(set_id) if self.knowledge else None
        bonuses = entry.get("bonuses") if isinstance(entry, Mapping) else None
        return bonuses if isinstance(bonuses, list) else ()

    def set_counts(self, inventory: Mapping[str, Any]) -> dict[str, int]:
        """背包各套装的件数（武器按族 id 归类）。"""
        counts: dict[str, int] = {}
        if self.knowledge is None:
            return counts
        for weapon in items(inventory.get("weapons")):
            family = self.family(str(weapon.get("id") or ""))
            for entry in self.knowledge.weapons.values():
                if entry.get("weapon_id") == family or entry.get("id") == family:
                    for set_id in entry.get("sets") or []:
                        counts[str(set_id)] = counts.get(str(set_id), 0) + 1
                    break
        return counts

    def need_value(
        self, entry: Mapping[str, Any], context: EconomyContext, profile: HeroProfile
    ) -> float:
        """短板补齐：正增量按相对缺口加权（只奖励补短板，不惩罚加长板）。"""
        deltas = entry.get("stat_deltas")
        if not isinstance(deltas, Mapping):
            return 0.0
        current = current_stats(context.stats)
        total = 0.0
        for key, raw in deltas.items():
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
                continue
            if not key.startswith(_STAT_PREFIX):
                continue
            stat = key[len(_STAT_PREFIX):]
            target = profile.targets.get(stat, 0.0)
            if target <= 0.0:
                continue
            deficit = max(0.0, (target - current.get(stat, 0.0)) / target)
            weight = profile.weights.get(stat, self.config.scoring.default_weight)
            total += weight * float(raw) * deficit
        return total * self.config.upgrade.need_weight

    def weakest_stat(self, context: EconomyContext) -> str:
        """当前相对缺口最大的属性（报告用）。"""
        profile = self.profile_weights(context.hero_id, context.inventory)
        current = current_stats(context.stats)
        best_stat = ""
        best_deficit = 0.0
        for stat, target in sorted(profile.targets.items()):
            if target <= 0.0:
                continue
            deficit = max(0.0, (target - current.get(stat, 0.0)) / target)
            if deficit > best_deficit:
                best_stat, best_deficit = stat, deficit
        return best_stat

    # ---- 阶段与金币成本 ----

    def stage(self, wave: int) -> str:
        shop = self.config.shop
        if wave <= shop.early_wave_max:
            return "early"
        if wave >= shop.late_wave_min:
            return "late"
        return "mid"

    def gold_cost(self, stage: str) -> float:
        shop = self.config.shop
        return {
            "early": shop.gold_cost_early,
            "mid": shop.gold_cost_mid,
            "late": shop.gold_cost_late,
        }[stage]
