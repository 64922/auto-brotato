"""decision 配置校验辅助：键/类型/数值范围（reflex 与 tactical 配置共用）。

各层配置模块（``config.py`` / ``tactical_config.py``）持有自己的异常类型
（``ReflexConfigError`` / ``TacticalConfigError``，均继承 :class:`DecisionConfigError`），
本模块只做通用校验：分区存在、键严格匹配、标量/数组类型与范围（bool 不算数字）。
错误信息统一为中文并带字段路径，便于定位配置问题。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence


class DecisionConfigError(ValueError):
    """决策层配置错误基类（缺失/类型非法/数值越界）。"""


def section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """取配置分区；缺失或非对象抛 :class:`DecisionConfigError`。"""
    value = raw.get(name)
    if not isinstance(value, Mapping):
        raise DecisionConfigError("缺少配置分区 %s（必须是对象）" % name)
    return value


def check_keys(section_map: Mapping[str, Any], expected: Sequence[str], where: str) -> None:
    """键严格匹配：多键/少键均报错（``where`` 为字段路径前缀）。"""
    expected_set = set(expected)
    missing = [key for key in expected if key not in section_map]
    extra = [key for key in section_map if key not in expected_set]
    if missing:
        raise DecisionConfigError("%s 缺少键：%s" % (where, "、".join(sorted(missing))))
    if extra:
        raise DecisionConfigError("%s 存在未知键：%s" % (where, "、".join(sorted(extra))))


def number(
    section_map: Mapping[str, Any],
    key: str,
    where: str,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    """取数字并校验范围（bool 不算数字）。"""
    value = section_map.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecisionConfigError("%s.%s 必须是数字，得到 %r" % (where, key, value))
    result = float(value)
    if result < minimum or (maximum is not None and result > maximum):
        bound = "[%g, %g]" % (minimum, maximum) if maximum is not None else ">= %g" % minimum
        raise DecisionConfigError("%s.%s=%g 超出范围 %s" % (where, key, result, bound))
    return result


def integer(
    section_map: Mapping[str, Any],
    key: str,
    where: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """取整数并校验范围（bool 不算整数）。"""
    value = section_map.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DecisionConfigError("%s.%s 必须是整数，得到 %r" % (where, key, value))
    if value < minimum or value > maximum:
        raise DecisionConfigError(
            "%s.%s=%d 超出范围 [%d, %d]" % (where, key, value, minimum, maximum)
        )
    return value


def boolean(section_map: Mapping[str, Any], key: str, where: str) -> bool:
    """取布尔值。"""
    value = section_map.get(key)
    if not isinstance(value, bool):
        raise DecisionConfigError("%s.%s 必须是布尔值，得到 %r" % (where, key, value))
    return value


def numbers(
    section_map: Mapping[str, Any], key: str, where: str, *, minimum: float
) -> tuple[float, ...]:
    """取数字数组并逐项校验下界；空数组报错。"""
    value = section_map.get(key)
    if not isinstance(value, (list, tuple)):
        raise DecisionConfigError("%s.%s 必须是数字数组，得到 %r" % (where, key, value))
    result = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise DecisionConfigError(
                "%s.%s[%d] 必须是数字，得到 %r" % (where, key, index, item)
            )
        item_number = float(item)
        if item_number < minimum:
            raise DecisionConfigError(
                "%s.%s[%d]=%g 超出范围 >= %g" % (where, key, index, item_number, minimum)
            )
        result.append(item_number)
    if not result:
        raise DecisionConfigError("%s.%s 不能为空" % (where, key))
    return tuple(result)
