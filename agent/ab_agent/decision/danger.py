"""危险场：危险源提取、预筛与势函数（strategy.md §3.1，票据 09）。

- 敌人（速度外推 + 半径）、敌方弹幕（vel 外推 + ttl）、地雷（静止）；
- 势函数 ``w·(radius/distance)²``（近距离钳制见 ``danger.min_distance_ratio``）；
- ``truncated=true`` 与紧急/无敌穿越的权重缩放在控制器侧传给 :func:`point_danger`；
- 敌人/弹幕外推使用快照 ``vel``；敌方弹幕实测 ``ttl=0`` 表示不截断（大于 0 才截断）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .config import ReflexConfig
from .geometry import Point, distance, items, point2, radius_of

KIND_ENEMY = "enemy"
KIND_PROJECTILE = "projectile"
KIND_HAZARD = "hazard"
KINDS = (KIND_ENEMY, KIND_PROJECTILE, KIND_HAZARD)


@dataclass(frozen=True)
class Threat:
    """危险场中的单个危险源（位置/速度/半径/存活时间）。"""

    kind: str
    pos: Point
    vel: Point
    radius: float
    ttl: Optional[float] = None


def threats_from_snapshot(snapshot: dict, config: ReflexConfig) -> tuple[Threat, ...]:
    """从快照提取危险源（敌人/敌方弹幕/地雷）；字段非法者跳过。"""
    threats: list[Threat] = []
    for enemy in items(snapshot.get("enemies")):
        pos = point2(enemy.get("pos"))
        if pos is None:
            continue
        threats.append(
            Threat(
                KIND_ENEMY,
                pos,
                point2(enemy.get("vel")) or (0.0, 0.0),
                radius_of(enemy, config.danger.enemy_default_radius),
                None,
            )
        )
    for projectile in items(snapshot.get("projectiles")):
        if projectile.get("friendly"):
            continue  # 玩家弹幕不构成危险
        pos = point2(projectile.get("pos"))
        if pos is None:
            continue
        ttl_raw = projectile.get("ttl")
        ttl = None
        if (
            isinstance(ttl_raw, (int, float))
            and not isinstance(ttl_raw, bool)
            and ttl_raw > 0.0
        ):
            ttl = float(ttl_raw)
        threats.append(
            Threat(
                KIND_PROJECTILE,
                pos,
                point2(projectile.get("vel")) or (0.0, 0.0),
                radius_of(projectile, config.danger.projectile_default_radius),
                ttl,
            )
        )
    for hazard in items(snapshot.get("hazards")):
        pos = point2(hazard.get("pos"))
        if pos is None:
            continue
        threats.append(
            Threat(
                KIND_HAZARD,
                pos,
                (0.0, 0.0),
                radius_of(hazard, config.danger.hazard_default_radius),
                None,
            )
        )
    return tuple(threats)


def select_threats(
    threats: tuple[Threat, ...], point: Point, config: ReflexConfig
) -> tuple[Threat, ...]:
    """性能预筛：只保留 ``consider_radius`` 内、每类最近的 ``max_threats_per_kind`` 个。

    危险势函数随距离平方衰减，远处威胁贡献可忽略；筛选保证高波次（300 敌/400 弹幕）
    下 30Hz 决策仍有性能余量（见票据 09 验证记录）。
    """
    radius = config.danger.consider_radius
    limit = config.danger.max_threats_per_kind
    selected: list[Threat] = []
    for kind in KINDS:
        group = [
            threat
            for threat in threats
            if threat.kind == kind and distance(point, threat.pos) <= radius
        ]
        if len(group) > limit:
            group.sort(key=lambda threat: distance(point, threat.pos))
            group = group[:limit]
        selected.extend(group)
    return tuple(selected)


def point_danger(
    threats: tuple[Threat, ...],
    point: Point,
    t: float,
    config: ReflexConfig,
    scales: Mapping[str, float],
) -> float:
    """``t`` 秒后 ``point`` 处来自各危险源的叠加代价。"""
    danger = config.danger
    params = {
        KIND_ENEMY: (danger.enemy_weight, danger.enemy_buffer),
        KIND_PROJECTILE: (danger.projectile_weight, danger.projectile_buffer),
        KIND_HAZARD: (danger.hazard_weight, danger.hazard_buffer),
    }
    total = 0.0
    for threat in threats:
        if threat.ttl is not None and t > threat.ttl:
            continue
        scale = scales.get(threat.kind, 0.0)
        if scale <= 0.0:
            continue
        weight, buffer = params[threat.kind]
        radius = threat.radius + config.player.radius + buffer
        if radius <= 0.0:
            continue
        dx = point[0] - (threat.pos[0] + threat.vel[0] * t)
        dy = point[1] - (threat.pos[1] + threat.vel[1] * t)
        gap = math.hypot(dx, dy)
        floor = radius * danger.min_distance_ratio
        if gap < floor:
            gap = floor
        total += scale * weight * (radius / gap) ** 2
    return total
