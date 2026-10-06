"""经济层参数配置：JSON 加载与结构校验（strategy.md §5/§8，票据 12）。

所有数值权重/阈值集中在 ``config/economy.json``；本模块只负责读取与校验，不做决策。
运行期可用 CLI ``--economy-config`` 指向替代文件，对同一回放调参对比。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .config_util import (
    DecisionConfigError,
    boolean as _boolean,
    check_keys as _check_keys,
    integer as _integer,
    number as _number,
    section as _section,
)

#: 默认参数文件（随包发布；所有键必填，缺失即报错）
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "economy.json"

#: Tier 评级（与 knowledge.TIER_GRADES 一致；此处校验配置完整性）
TIER_GRADES = ("S", "A", "B", "C", "D")

#: 英雄/武器类别兜底（``hero_assign.by_class`` 的键）
WEAPON_CLASSES = ("ranged", "melee")

#: 配置分区名（多/少键均报错）
_SECTIONS = ("scoring", "shop", "upgrade", "heroes", "hero_assign")
_SECTION_KEYS = {
    "scoring": (
        "default_weight",
        "weapon_dps_weight",
        "merge_bonus",
        "set_bonus_scale",
        "weapon_full_penalty",
        "tier_bonus",
        "effect_weights",
    ),
    "shop": (
        "buy_threshold",
        "early_wave_max",
        "late_wave_min",
        "gold_cost_early",
        "gold_cost_mid",
        "gold_cost_late",
        "safety_margin_early",
        "safety_margin_mid",
        "safety_margin_late",
        "reroll_threshold",
        "reroll_budget_base",
        "reroll_budget_per_wave",
        "lock_min_value",
        "lock_enabled",
        "max_weapon_slots",
        "sell_min_weapons",
        "sell_below_score",
    ),
    "upgrade": ("need_weight", "unknown_score"),
    "heroes": (),
    "hero_assign": ("heroes", "fallback", "by_class"),
}


class EconomyConfigError(DecisionConfigError):
    """经济层配置缺失/类型非法/数值越界（中文信息，带字段路径）。"""


@dataclass(frozen=True)
class ScoringConfig:
    """通用评分：属性权重兜底、武器 DPS、协同（合并/套装）、Tier 与满位惩罚。"""

    default_weight: float
    weapon_dps_weight: float
    merge_bonus: float
    set_bonus_scale: float
    weapon_full_penalty: float
    tier_bonus: Mapping[str, float]
    effect_weights: Mapping[str, float]


@dataclass(frozen=True)
class ShopConfig:
    """商店规则：购买阈值/预算、刷新预算曲线、锁定与出售条件。"""

    buy_threshold: float
    early_wave_max: int
    late_wave_min: int
    gold_cost_early: float
    gold_cost_mid: float
    gold_cost_late: float
    safety_margin_early: float
    safety_margin_mid: float
    safety_margin_late: float
    reroll_threshold: float
    reroll_budget_base: float
    reroll_budget_per_wave: float
    lock_min_value: float
    lock_enabled: bool
    max_weapon_slots: int
    sell_min_weapons: int
    sell_below_score: float


@dataclass(frozen=True)
class UpgradeConfig:
    """升级选卡：短板补齐权重与未知卡兜底分。"""

    need_weight: float
    unknown_score: float


@dataclass(frozen=True)
class HeroProfile:
    """英雄/构筑权重表：``weights`` 为属性/效果每点价值；``targets`` 为短板目标值。"""

    weights: Mapping[str, float]
    targets: Mapping[str, float]


@dataclass(frozen=True)
class HeroAssign:
    """英雄 → 构筑档案映射；未知英雄按武器类别兜底。"""

    heroes: Mapping[str, str]
    fallback: str
    by_class: Mapping[str, str]


@dataclass(frozen=True)
class EconomyConfig:
    """经济层完整参数集。"""

    scoring: ScoringConfig
    shop: ShopConfig
    upgrade: UpgradeConfig
    heroes: Mapping[str, HeroProfile]
    hero_assign: HeroAssign


def load_economy_config(path: str | Path | None = None) -> EconomyConfig:
    """读取并校验配置文件；``path=None`` 用包内默认 ``config/economy.json``。"""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EconomyConfigError("无法读取配置文件 %s：%s" % (config_path, exc)) from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EconomyConfigError("配置文件 %s 非法 JSON：%s" % (config_path, exc)) from exc
    if not isinstance(raw, dict):
        raise EconomyConfigError("配置文件 %s 顶层必须是 JSON 对象" % config_path)
    return economy_config_from_mapping(raw)


def economy_config_from_mapping(raw: Mapping[str, Any]) -> EconomyConfig:
    """由 mapping 构造 :class:`EconomyConfig`；键缺失/类型或数值非法抛错。"""
    try:
        return _build_economy_config(raw)
    except DecisionConfigError as exc:
        raise EconomyConfigError(str(exc)) from exc


def _build_economy_config(raw: Mapping[str, Any]) -> EconomyConfig:
    if not isinstance(raw, Mapping):
        raise EconomyConfigError("配置必须是对象")
    _check_keys(raw, _SECTIONS, "顶层")
    for name in _SECTIONS:
        if _SECTION_KEYS[name]:
            _check_keys(_section(raw, name), _SECTION_KEYS[name], name)

    scoring = _build_scoring(_section(raw, "scoring"))
    shop = _build_shop(_section(raw, "shop"))
    upgrade = _build_upgrade(_section(raw, "upgrade"))
    heroes = _build_heroes(_section(raw, "heroes"))
    hero_assign = _build_hero_assign(_section(raw, "hero_assign"), heroes)
    return EconomyConfig(
        scoring=scoring,
        shop=shop,
        upgrade=upgrade,
        heroes=heroes,
        hero_assign=hero_assign,
    )


def _build_scoring(raw: Mapping[str, Any]) -> ScoringConfig:
    tier_raw = _section(raw, "tier_bonus")
    _check_keys(tier_raw, TIER_GRADES, "scoring.tier_bonus")
    tier_bonus = {
        grade: _number(tier_raw, grade, "scoring.tier_bonus", minimum=-1000.0, maximum=1000.0)
        for grade in TIER_GRADES
    }
    return ScoringConfig(
        default_weight=_number(raw, "default_weight", "scoring", minimum=-1000.0, maximum=1000.0),
        weapon_dps_weight=_number(
            raw, "weapon_dps_weight", "scoring", minimum=0.0, maximum=1000.0
        ),
        merge_bonus=_number(raw, "merge_bonus", "scoring", minimum=0.0, maximum=1000.0),
        set_bonus_scale=_number(raw, "set_bonus_scale", "scoring", minimum=0.0, maximum=1000.0),
        weapon_full_penalty=_number(
            raw, "weapon_full_penalty", "scoring", minimum=0.0, maximum=1000.0
        ),
        tier_bonus=tier_bonus,
        effect_weights=_number_map(raw, "effect_weights", "scoring"),
    )


def _build_shop(raw: Mapping[str, Any]) -> ShopConfig:
    shop = ShopConfig(
        buy_threshold=_number(raw, "buy_threshold", "shop", minimum=-1000.0, maximum=1000.0),
        early_wave_max=_integer(raw, "early_wave_max", "shop", minimum=0, maximum=100),
        late_wave_min=_integer(raw, "late_wave_min", "shop", minimum=1, maximum=200),
        gold_cost_early=_number(raw, "gold_cost_early", "shop", minimum=0.0, maximum=1.0),
        gold_cost_mid=_number(raw, "gold_cost_mid", "shop", minimum=0.0, maximum=1.0),
        gold_cost_late=_number(raw, "gold_cost_late", "shop", minimum=0.0, maximum=1.0),
        safety_margin_early=_number(
            raw, "safety_margin_early", "shop", minimum=0.0, maximum=100000.0
        ),
        safety_margin_mid=_number(
            raw, "safety_margin_mid", "shop", minimum=0.0, maximum=100000.0
        ),
        safety_margin_late=_number(
            raw, "safety_margin_late", "shop", minimum=0.0, maximum=100000.0
        ),
        reroll_threshold=_number(
            raw, "reroll_threshold", "shop", minimum=-1000.0, maximum=1000.0
        ),
        reroll_budget_base=_number(
            raw, "reroll_budget_base", "shop", minimum=0.0, maximum=100.0
        ),
        reroll_budget_per_wave=_number(
            raw, "reroll_budget_per_wave", "shop", minimum=0.0, maximum=10.0
        ),
        lock_min_value=_number(raw, "lock_min_value", "shop", minimum=-1000.0, maximum=1000.0),
        lock_enabled=_boolean(raw, "lock_enabled", "shop"),
        max_weapon_slots=_integer(raw, "max_weapon_slots", "shop", minimum=1, maximum=64),
        sell_min_weapons=_integer(raw, "sell_min_weapons", "shop", minimum=0, maximum=64),
        sell_below_score=_number(
            raw, "sell_below_score", "shop", minimum=-1000.0, maximum=1000.0
        ),
    )
    if shop.early_wave_max >= shop.late_wave_min:
        raise EconomyConfigError("shop.early_wave_max 必须小于 late_wave_min")
    if shop.sell_min_weapons > shop.max_weapon_slots:
        raise EconomyConfigError("shop.sell_min_weapons 不能大于 max_weapon_slots")
    return shop


def _build_upgrade(raw: Mapping[str, Any]) -> UpgradeConfig:
    return UpgradeConfig(
        need_weight=_number(raw, "need_weight", "upgrade", minimum=0.0, maximum=1000.0),
        unknown_score=_number(
            raw, "unknown_score", "upgrade", minimum=-1000.0, maximum=1000.0
        ),
    )


def _build_heroes(raw: Mapping[str, Any]) -> Mapping[str, HeroProfile]:
    if not raw:
        raise EconomyConfigError("heroes 不能为空")
    profiles: dict[str, HeroProfile] = {}
    for name, profile_raw in raw.items():
        if not isinstance(profile_raw, Mapping):
            raise EconomyConfigError("heroes.%s 必须是对象" % name)
        _check_keys(profile_raw, ("weights", "targets"), "heroes.%s" % name)
        profiles[name] = HeroProfile(
            weights=_number_map(profile_raw, "weights", "heroes.%s" % name),
            targets=_number_map(
                profile_raw, "targets", "heroes.%s" % name, minimum=0.0
            ),
        )
    return profiles


def _build_hero_assign(
    raw: Mapping[str, Any], heroes: Mapping[str, HeroProfile]
) -> HeroAssign:
    heroes_raw = raw.get("heroes")
    if not isinstance(heroes_raw, Mapping):
        raise EconomyConfigError("hero_assign.heroes 必须是对象")
    by_class_raw = raw.get("by_class")
    if not isinstance(by_class_raw, Mapping):
        raise EconomyConfigError("hero_assign.by_class 必须是对象")
    _check_keys(by_class_raw, WEAPON_CLASSES, "hero_assign.by_class")
    fallback = _profile_name(raw.get("fallback"), heroes, "hero_assign.fallback")
    assignments: dict[str, str] = {}
    for hero_id, name in heroes_raw.items():
        assignments[str(hero_id)] = _profile_name(
            name, heroes, "hero_assign.heroes.%s" % hero_id
        )
    by_class = {
        weapon_class: _profile_name(
            by_class_raw.get(weapon_class), heroes, "hero_assign.by_class.%s" % weapon_class
        )
        for weapon_class in WEAPON_CLASSES
    }
    return HeroAssign(heroes=assignments, fallback=fallback, by_class=by_class)


def _profile_name(value: Any, heroes: Mapping[str, HeroProfile], where: str) -> str:
    if not isinstance(value, str) or not value:
        raise EconomyConfigError("%s 必须是非空字符串" % where)
    if value not in heroes:
        raise EconomyConfigError("%s 指向未定义的英雄档案 %r" % (where, value))
    return value


def _number_map(
    section_map: Mapping[str, Any], key: str, where: str, *, minimum: float = -1e9
) -> Mapping[str, float]:
    """取 ``键 → 数字`` 映射（键非空字符串、值数字且不低于下界）。"""
    value = section_map.get(key)
    if not isinstance(value, Mapping) or not value:
        raise EconomyConfigError("%s.%s 必须是非空对象" % (where, key))
    result: dict[str, float] = {}
    for entry_key, entry_value in value.items():
        if not isinstance(entry_key, str) or not entry_key:
            raise EconomyConfigError("%s.%s 的键必须是非空字符串" % (where, key))
        if isinstance(entry_value, bool) or not isinstance(entry_value, (int, float)):
            raise EconomyConfigError(
                "%s.%s.%s 必须是数字，得到 %r" % (where, key, entry_key, entry_value)
            )
        number = float(entry_value)
        if number < minimum:
            raise EconomyConfigError(
                "%s.%s.%s=%g 超出范围 >= %g" % (where, key, entry_key, number, minimum)
            )
        result[entry_key] = number
    return result
