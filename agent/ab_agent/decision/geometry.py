"""decision 子包共用的协议字段解析与几何辅助（配置无关、无 IO）。

快照字段按 docs/protocol.md §3.1 解析；非法/缺失一律安全降级（返回 None/默认值），
决策模块因此不需要在每处访问点重复校验。
"""
from __future__ import annotations

import math
from typing import Any, Optional, Sequence

Point = tuple[float, float]
Band = tuple[float, float]
Arena = tuple[float, float, float, float]

Number = (int, float)


def items(value: Any) -> Sequence[dict]:
    """取快照中的对象数组（非数组/非对象元素丢弃）。"""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def point2(value: Any) -> Optional[Point]:
    """解析 ``[x, y]`` 坐标；非法返回 None（bool 不算数字）。"""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    x, y = value[0], value[1]
    if (
        isinstance(x, bool)
        or isinstance(y, bool)
        or not isinstance(x, Number)
        or not isinstance(y, Number)
    ):
        return None
    return (float(x), float(y))


def radius_of(item: dict, default: float) -> float:
    """实体半径；缺失/非法用分类默认值，负值归零。"""
    value = item.get("radius")
    if isinstance(value, bool) or not isinstance(value, Number):
        return default
    return max(float(value), 0.0)


def distance(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def arena_rect(value: Any) -> Optional[Arena]:
    """解析 ``{min, max}`` 场地矩形为 ``(min_x, min_y, max_x, max_y)``。"""
    if not isinstance(value, dict):
        return None
    low = point2(value.get("min"))
    high = point2(value.get("max"))
    if low is None or high is None or low[0] >= high[0] or low[1] >= high[1]:
        return None
    return (low[0], low[1], high[0], high[1])


def band_pair(value: Any) -> Optional[Band]:
    """解析距离带 ``[min, max]``；必须为正且递增。"""
    parsed = point2(value)
    if parsed is None or parsed[0] <= 0.0 or parsed[0] >= parsed[1]:
        return None
    return parsed
