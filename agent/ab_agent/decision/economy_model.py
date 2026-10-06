"""经济层数据结构与观测辅助（无决策逻辑；economy.py / economy_runner.py 共用）。

数据流：``EconomyContext``（观测派生）→ 评分（``economy_scoring``）→
``Appraisal`` → ``ShopPlan`` / ``UpgradePlan``（建议动作）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

#: 观测 stats 字段 → 知识库属性键（协议 §3.1.1 vs 知识库 ``stat_*``）
STAT_ALIASES = {"hp_regen": "hp_regeneration"}


@dataclass(frozen=True)
class EconomyContext:
    """一次经济决策的对局上下文（观测派生）。"""

    wave: int
    gold: int
    stats: Mapping[str, float] = field(default_factory=dict)
    inventory: Mapping[str, Any] = field(default_factory=dict)
    hero_id: str = ""


@dataclass(frozen=True)
class ScoreBreakdown:
    """评分明细（可解释、可写回放）。"""

    stat_value: float = 0.0
    tier: float = 0.0
    synergy: float = 0.0
    need: float = 0.0
    price: float = 0.0
    flags: tuple[str, ...] = ()

    @property
    def total(self) -> float:
        return self.stat_value + self.tier + self.synergy + self.need + self.price

    def format(self) -> str:
        parts = [
            "属性 %.1f" % self.stat_value,
            "Tier %+.1f" % self.tier,
            "协同 %+.1f" % self.synergy,
            "短板 %+.1f" % self.need,
            "价格 %+.1f" % self.price,
        ]
        text = " = ".join(["评分 %.1f" % self.total, " + ".join(parts)])
        if self.flags:
            text += "（%s）" % "、".join(self.flags)
        return text


@dataclass(frozen=True)
class Appraisal:
    """一件商品/一张卡的评分结果。"""

    slot: int
    kind: str
    id: str
    tier: int
    price: int
    score: ScoreBreakdown
    known: bool

    @property
    def label(self) -> str:
        return self.id or "未知"


@dataclass(frozen=True)
class ShopPlan:
    """商店建议动作。``target_key`` 供失败抑制（如 ``shop_buy:2``）。"""

    action: str
    params: Mapping[str, Any]
    reason: str
    target_key: str = ""
    appraisals: tuple[Appraisal, ...] = ()


@dataclass(frozen=True)
class UpgradePlan:
    """升级选卡建议。``slot=None`` 表示没有可选卡。"""

    slot: int | None
    params: Mapping[str, Any]
    reason: str
    target_key: str = ""
    appraisals: tuple[Appraisal, ...] = ()


def items(value: Any) -> list[Mapping[str, Any]]:
    """防御式取对象数组（非列表/含非对象项时安全退化）。"""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def as_int(value: Any) -> int | None:
    """收敛为 int（bool/字符串等非法值返回 None）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def current_stats(stats: Mapping[str, float]) -> dict[str, float]:
    """观测 stats → 知识库属性键（``hp_regen`` → ``hp_regeneration``）。"""
    current: dict[str, float] = {}
    if not isinstance(stats, Mapping):
        return current
    for key, value in stats.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        current[STAT_ALIASES.get(str(key), str(key))] = float(value)
    return current
