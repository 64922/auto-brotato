"""菜单观测视图：难度页/终局/商店/升级的解析与终端文案（票据 07）。

纯数据类与格式化函数，不做 IO、不持有状态；字段来源见 docs/protocol.md §7.1。
解析全部防御式处理：缺字段/类型不对时退化为安全默认值，不抛异常。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .hero_names import hero_display

#: 模式开关（key, 短标签, 长标签；protocol.md §7.1.1 `modes`）
MODE_LABELS = (
    ("endless", "无尽", "无尽模式"),
    ("ban", "禁用", "禁用模式"),
    ("coop", "合作", "合作模式"),
)

#: `difficulty.my_id` 前缀（protocol.md §7.1.1）
DIFFICULTY_PREFIX = "difficulty_"

#: 终局页统计中值得进战报的关键属性（protocol.md §7.1.3 的 StatsContainer key）
KEY_STATS = {
    "STAT_MAX_HP": "最大生命",
    "STAT_RANGED_DAMAGE": "远程伤害",
    "STAT_MELEE_DAMAGE": "近战伤害",
    "STAT_ATTACK_SPEED": "攻击速度",
    "STAT_CRIT_CHANCE": "暴击率",
    "STAT_ARMOR": "护甲",
    "STAT_DODGE": "闪避",
    "STAT_SPEED": "移动速度",
}


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def difficulty_my_id(value: int) -> str:
    """难度值的 ``my_id`` 串（``difficulty_<value>``）。"""
    return DIFFICULTY_PREFIX + str(value)


def difficulty_value(my_id: Any) -> Optional[int]:
    """从 ``difficulty_<n>`` 解析数值；不是该形式返回 None。"""
    if not isinstance(my_id, str) or not my_id.startswith(DIFFICULTY_PREFIX):
        return None
    suffix = my_id[len(DIFFICULTY_PREFIX) :]
    return int(suffix) if suffix.isdigit() else None


def _difficulty_value(option: dict) -> Optional[int]:
    value = _as_int(option.get("value"))
    if value is not None:
        return value
    return difficulty_value(option.get("my_id"))


@dataclass(frozen=True)
class DifficultyOption:
    """难度选项（protocol.md §7.1.1）。"""

    value: int
    my_id: str
    name: str = ""
    tier: int = 0
    is_locked: bool = True
    unlocked_by_default: bool = False


@dataclass(frozen=True)
class DifficultyMenu:
    """难度选择页视图。"""

    hero_id: str
    weapons: tuple[dict, ...]
    options: tuple[DifficultyOption, ...]
    selected: Optional[str]
    modes: dict
    can_start: bool

    @classmethod
    def from_payload(cls, payload: dict) -> "DifficultyMenu":
        character = payload.get("character")
        hero_id = ""
        if isinstance(character, dict) and isinstance(character.get("id"), str):
            hero_id = character["id"]
        weapons = tuple(
            weapon for weapon in payload.get("weapons") or [] if isinstance(weapon, dict)
        )
        options = []
        difficulty = payload.get("difficulty")
        raw_options = difficulty.get("options") if isinstance(difficulty, dict) else None
        for option in raw_options or []:
            if not isinstance(option, dict):
                continue
            value = _difficulty_value(option)
            if value is None:
                continue
            options.append(
                DifficultyOption(
                    value=value,
                    my_id=str(option.get("my_id") or difficulty_my_id(value)),
                    name=str(option.get("name") or ""),
                    tier=_as_int(option.get("tier")) or 0,
                    is_locked=option.get("is_locked") is True,
                    unlocked_by_default=option.get("unlocked_by_default") is True,
                )
            )
        selected = difficulty.get("selected") if isinstance(difficulty, dict) else None
        modes = payload.get("modes")
        return cls(
            hero_id=hero_id,
            weapons=weapons,
            options=tuple(options),
            selected=selected if isinstance(selected, str) else None,
            modes=modes if isinstance(modes, dict) else {},
            can_start=payload.get("can_start") is True,
        )

    # ---- 派生信息 ----

    @property
    def unlocked_values(self) -> tuple[int, ...]:
        """已解锁难度（`is_locked == false`）升序；空表示读不到解锁信息。"""
        return tuple(sorted(option.value for option in self.options if not option.is_locked))

    @property
    def max_unlocked(self) -> Optional[int]:
        values = self.unlocked_values
        return values[-1] if values else None

    def is_unlocked(self, value: int) -> bool:
        return value in self.unlocked_values

    def unlocked_range_text(self) -> str:
        values = self.unlocked_values
        if not values:
            return "未知（未读到解锁信息）"
        if values == tuple(range(values[0], values[-1] + 1)):
            if values[0] == values[-1]:
                return "D%d" % values[0]
            return "D%d–D%d" % (values[0], values[-1])
        return "、".join("D%d" % value for value in values)

    def selected_value(self) -> Optional[int]:
        return difficulty_value(self.selected)

    def selected_text(self) -> str:
        value = self.selected_value()
        return "D%d" % value if value is not None else "无"

    def modes_on(self) -> bool:
        return any(self.modes.get(key) is True for key, _, _ in MODE_LABELS)

    def modes_text(self) -> str:
        return " · ".join(
            "%s=%s" % (short, "开" if self.modes.get(key) is True else "关")
            for key, short, _ in MODE_LABELS
        )

    def modes_on_text(self) -> str:
        return "、".join(
            long for key, _, long in MODE_LABELS if self.modes.get(key) is True
        )

    # ---- 终端文案 ----

    def format_prompt(self) -> str:
        weapons = "、".join(_weapon_text(weapon) for weapon in self.weapons) or "未知"
        lines = [
            "检测到难度选择页：",
            "  英雄：%s" % hero_display(self.hero_id),
            "  初始武器：%s" % weapons,
            "  可选难度：%s" % self.unlocked_range_text(),
            "  模式开关：%s" % self.modes_text(),
            "  当前选择：%s" % self.selected_text(),
            "请输入难度（如 D0，回车重打印、q 取消）：",
        ]
        return "\n".join(lines)

    def format_confirm_prompt(self, value: int) -> str:
        return (
            "模式开关已开启：%s。确认按此模式开始 D%d 对局？"
            "（y=开始 / n=返回 / q=取消）" % (self.modes_on_text(), value)
        )


def _weapon_text(weapon: dict) -> str:
    my_id = str(weapon.get("my_id") or "")
    weapon_id = str(weapon.get("weapon_id") or "")
    tier = _as_int(weapon.get("tier"))
    label = my_id or weapon_id or "未知"
    detail = weapon_id if weapon_id and weapon_id != label else ""
    if detail and tier is not None:
        return "%s（%s·T%d）" % (label, detail, tier)
    if detail:
        return "%s（%s）" % (label, detail)
    if tier is not None:
        return "%s（T%d）" % (label, tier)
    return label


@dataclass(frozen=True)
class RunEndMenu:
    """终局页视图（protocol.md §7.1.3）。"""

    result: Optional[str]
    wave: Optional[int]
    title: str
    stats: dict = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict) -> "RunEndMenu":
        result = payload.get("result")
        if result not in ("victory", "defeat"):
            result = None
        stats = payload.get("stats")
        return cls(
            result=result,
            wave=_as_int(payload.get("wave")),
            title=str(payload.get("title") or ""),
            stats=stats if isinstance(stats, dict) else {},
        )

    def result_text(self) -> str:
        return result_text(self.result)


def result_text(result: Optional[str]) -> str:
    """结果枚举 → 中文（终局页与战报共用）。"""
    if result == "victory":
        return "胜利"
    if result == "defeat":
        return "战败"
    return "未知"


@dataclass
class BattleReport:
    """战报数据（由 RunController 在终局时汇总，RunSession 打印）。"""

    result: Optional[str]
    wave: Optional[int]
    duration_s: Optional[float]
    gold: Optional[int]
    materials: Optional[int]
    weapons: tuple[dict, ...] = ()
    items: tuple[dict, ...] = ()
    stats: dict = field(default_factory=dict)
    replay_path: Optional[str] = None
    note: Optional[str] = None

    def format(self) -> str:
        lines = ["========== 对局战报 =========="]
        result = result_text(self.result)
        if self.result:
            lines.append("结果：%s（%s）" % (result, self.result))
        else:
            lines.append("结果：%s" % result)
        lines.append("波次：%s" % ("第 %d 波" % self.wave if self.wave is not None else "未知"))
        lines.append("时长：%s" % format_duration(self.duration_s))
        gold = "未知" if self.gold is None else str(self.gold)
        if self.materials is not None:
            gold += "（最近一波材料 %d）" % self.materials
        lines.append("金币：%s" % gold)
        lines.append("构建：武器 %d 把 · 道具 %d 件" % (len(self.weapons), len(self.items)))
        weapon_text = " · ".join(_inventory_weapon_text(weapon) for weapon in self.weapons)
        if weapon_text:
            lines.append("  武器：%s" % weapon_text)
        item_text = " · ".join(_inventory_item_text(item) for item in self.items)
        if item_text:
            lines.append("  道具：%s" % item_text)
        key_stats = [
            "%s=%s" % (KEY_STATS[key], self.stats[key])
            for key in KEY_STATS
            if self.stats.get(key) not in (None, "")
        ]
        if key_stats:
            lines.append("关键属性：%s" % " · ".join(key_stats))
        lines.append("回放：%s" % (self.replay_path or "未录制"))
        if self.note:
            lines.append("备注：%s" % self.note)
        lines.append("==============================")
        return "\n".join(lines)


def _inventory_weapon_text(weapon: dict) -> str:
    weapon_id = str(weapon.get("id") or "未知")
    tier = _as_int(weapon.get("tier"))
    return "%s(T%d)" % (weapon_id, tier) if tier is not None else weapon_id


def _inventory_item_text(item: dict) -> str:
    item_id = str(item.get("id") or "未知")
    count = _as_int(item.get("count")) or 1
    return "%s×%d" % (item_id, count) if count > 1 else item_id


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "未知"
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    if minutes:
        return "%d 分 %d 秒" % (minutes, secs)
    return "%d 秒" % secs


def format_shop_summary(shop: dict) -> str:
    """商店固定动作日志（票据 07 占位：只评估打印后离开）。"""
    slots = shop.get("slots") or []
    return "[商店] 下一波=%s · 金币=%s · 商品 %d 件 → 固定动作：离开" % (
        shop.get("wave_next"),
        shop.get("gold"),
        len(slots),
    )


def format_level_up_summary(payload: dict) -> str:
    """升级页固定动作日志（票据 07 占位：选第一张可选卡）。"""
    options = payload.get("options") or []
    cards = [
        "%s=%s" % (option.get("slot"), option.get("id") or option.get("kind") or "?")
        for option in options
        if isinstance(option, dict)
    ]
    return "[升级] 第 %s 波 · %d 张卡：%s" % (
        payload.get("wave"),
        len(options),
        " · ".join(cards) or "无",
    )


def first_pickable_slot(payload: dict) -> Optional[int]:
    """升级页第一张可选的卡槽位；没有可选卡返回 None。"""
    for option in payload.get("options") or []:
        if not isinstance(option, dict):
            continue
        slot = _as_int(option.get("slot"))
        if slot is not None and option.get("can_pick") is True:
            return slot
    return None
