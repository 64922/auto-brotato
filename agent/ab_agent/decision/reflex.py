"""反射层控制器：候选方向评分、平滑与输出（strategy.md §3，票据 09）。

危险源提取与势函数在 :mod:`ab_agent.decision.danger`，协议解析/几何在
:mod:`ab_agent.decision.geometry`；本模块只负责决策：

- 时间窗多采样（默认 0.15/0.3/0.5/0.8s）按衰减叠加为方向代价；
- 候选方向（默认 16 个 + 上一方向）评分：
  ``score = −危险 − 边界 − 距离带 − 期望位置距离 − 转向惩罚``；
- 输出向量经指数平滑与模长钳制，默认 30Hz 重算，避免 8 向 PWM 下的抖振；
- 低血紧急策略（提高危险权重、优先回血消耗品）与 ``invuln`` 穿越弹幕（可配置）；
- ``truncated=true`` 时放大危险权重（保守）；
- 战术层（票据 10）通过 :class:`TacticalIntent` 提供期望位置/距离带/权重；缺省时使用
  默认目标（最近材料，低血优先回血消耗品，否则场地中心）。

本模块无 IO、无随机：时间由调用方传入，回放/实机可以完全复现（ADR-0008）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from .config import ReflexConfig, load_reflex_config
from .danger import (
    KIND_ENEMY,
    KIND_PROJECTILE,
    KINDS,
    Threat,
    point_danger,
    select_threats,
    threats_from_snapshot,
)
from .geometry import Band, Point, arena_rect, band_pair, distance, items, point2

_PICKUP_MATERIAL = "material"
_PICKUP_CONSUMABLE = "consumable"


@dataclass(frozen=True)
class TacticalIntent:
    """战术层交给反射层的意图（票据 10）；全部字段可选，缺省走内置默认目标。

    - ``expected_position``：期望位置（可选；给定时优先于默认材料/中心目标）；
    - ``distance_band``：目标距离带（可选；缺省用配置的武器射程默认带）；
    - ``danger_scale``：危险权重整体缩放（>1 更保守）；
    - ``attraction_scale``：吸附/收益权重的整体缩放（<1 更专注生存）。
    """

    expected_position: Optional[Point] = None
    distance_band: Optional[Band] = None
    danger_scale: float = 1.0
    attraction_scale: float = 1.0


class ReflexController:
    """反射层控制器：``next_move(snapshot, now)`` 返回 move 向量或 None（未到重算点）。"""

    def __init__(self, config: Optional[ReflexConfig] = None) -> None:
        self._config = config or load_reflex_config()
        self.reset()

    def reset(self) -> None:
        """新对局/停止时清空平滑与节流状态。"""
        self._output: Optional[Point] = None
        self._last_dir: Optional[Point] = None
        self._last_decision_at: Optional[float] = None

    # ---- 对外接口 ----

    def next_move(
        self,
        snapshot: dict,
        now: float,
        intent: Optional[TacticalIntent] = None,
    ) -> Optional[list[float]]:
        """到重算间隔（``decision.interval_s``）时返回 move 向量，否则 None。"""
        if not isinstance(snapshot, dict):
            return None
        player = snapshot.get("player")
        if not isinstance(player, dict) or player.get("alive") is False:
            return None
        pos = point2(player.get("pos"))
        if pos is None:
            return None
        if (
            self._last_decision_at is not None
            and now - self._last_decision_at < self._config.decision.interval_s
        ):
            return None
        self._last_decision_at = now
        context = self._build_context(snapshot, player, pos, intent)
        chosen = self._choose(context)
        vector = self._smooth(chosen)
        self._last_dir = chosen
        return [round(vector[0], 3), round(vector[1], 3)]

    def danger_at(self, snapshot: dict, point: Sequence[float]) -> float:
        """危险场在 ``point`` 的瞬时代价（供战术层评估目标位与测试）。"""
        target = point2(point)
        if target is None or not isinstance(snapshot, dict):
            return 0.0
        threats = select_threats(
            threats_from_snapshot(snapshot, self._config), target, self._config
        )
        base = self._config.danger.truncated_scale if snapshot.get("truncated") else 1.0
        scales = {kind: base for kind in KINDS}
        return point_danger(threats, target, 0.0, self._config, scales)

    # ---- 上下文构建 ----

    def _build_context(
        self,
        snapshot: dict,
        player: dict,
        pos: Point,
        intent: Optional[TacticalIntent],
    ) -> "_Context":
        config = self._config
        threats = select_threats(threats_from_snapshot(snapshot, config), pos, config)
        enemies = tuple(threat for threat in threats if threat.kind == KIND_ENEMY)

        emergency = _is_emergency(player, config)
        danger_scale = intent.danger_scale if intent is not None else 1.0
        danger_scale = max(danger_scale, 0.0)
        if emergency:
            danger_scale *= config.emergency.danger_scale
        base = config.danger.truncated_scale if snapshot.get("truncated") else 1.0
        scales = {kind: base * danger_scale for kind in KINDS}
        if (
            config.invuln.enabled
            and player.get("invuln") is True
            and _nearest_distance(pos, enemies) < config.invuln.trapped_distance
        ):
            scales[KIND_PROJECTILE] *= config.invuln.crossing_scale

        target, target_weight = self._select_target(snapshot, pos, intent, emergency)
        band = config.kiting.default_band
        if intent is not None and intent.distance_band is not None:
            parsed = band_pair(intent.distance_band)
            if parsed is not None:
                band = parsed
        return _Context(
            pos=pos,
            speed=_player_speed(player, config),
            threats=threats,
            enemies=enemies,
            danger_scales=scales,
            samples=tuple(
                zip(config.danger.sample_times_s, config.danger.sample_decay)
            ),
            target=target,
            target_weight=target_weight,
            band=band,
            arena=arena_rect(snapshot.get("arena")),
            prev_dir=self._last_dir,
        )

    def _select_target(
        self,
        snapshot: dict,
        pos: Point,
        intent: Optional[TacticalIntent],
        emergency: bool,
    ) -> tuple[Optional[Point], float]:
        config = self._config
        attraction_scale = 1.0
        if intent is not None:
            if intent.expected_position is not None:
                expected = point2(intent.expected_position)
                if expected is not None:
                    return expected, config.tactical.expected_weight * max(
                        intent.attraction_scale, 0.0
                    )
            attraction_scale = max(intent.attraction_scale, 0.0)

        wanted = _PICKUP_CONSUMABLE if emergency else _PICKUP_MATERIAL
        best_point: Optional[Point] = None
        best_distance = math.inf
        for pickup in items(snapshot.get("pickups")):
            if pickup.get("kind") != wanted:
                continue
            point = point2(pickup.get("pos"))
            if point is None:
                continue
            gap = distance(pos, point)
            if gap > config.tactical.pickup_max_distance or gap >= best_distance:
                continue
            best_point, best_distance = point, gap
        if best_point is not None:
            weight = (
                config.tactical.consumable_weight
                if emergency
                else config.tactical.pickup_weight
            )
            return best_point, weight * attraction_scale

        arena = arena_rect(snapshot.get("arena"))
        if arena is not None:
            center = ((arena[0] + arena[2]) / 2.0, (arena[1] + arena[3]) / 2.0)
            scale = attraction_scale * (config.emergency.attraction_scale if emergency else 1.0)
            return center, config.tactical.center_weight * scale
        return None, 0.0

    # ---- 方向评分 ----

    def _choose(self, context: "_Context") -> Point:
        config = self._config
        best_dir: Optional[Point] = None
        best_score = -math.inf
        for direction in _candidates(config.decision.candidate_count, context.prev_dir):
            score = -self._direction_cost(direction, context)
            if context.prev_dir is not None:
                turn = _angle_between(direction, context.prev_dir) / math.pi
                score -= config.decision.turn_penalty * turn
            if score > best_score:
                best_score = score
                best_dir = direction
        return best_dir if best_dir is not None else (1.0, 0.0)

    def _direction_cost(self, direction: Point, context: "_Context") -> float:
        config = self._config
        total = 0.0
        for t, decay in context.samples:
            point = (
                context.pos[0] + direction[0] * context.speed * t,
                context.pos[1] + direction[1] * context.speed * t,
            )
            total += decay * point_danger(
                context.threats, point, t, config, context.danger_scales
            )
            if context.arena is not None:
                total += decay * _boundary_cost(point, context.arena, config)
            if context.target is not None:
                total += (
                    decay
                    * context.target_weight
                    * distance(point, context.target)
                    / config.tactical.attraction_range
                )
            if context.enemies:
                total += decay * _band_cost(point, t, context, config)
        return total

    # ---- 输出平滑 ----

    def _smooth(self, chosen: Point) -> Point:
        config = self._config
        decision = config.decision
        if self._output is None:
            output = chosen
        else:
            alpha = decision.smoothing_alpha
            output = (
                chosen[0] * alpha + self._output[0] * (1.0 - alpha),
                chosen[1] * alpha + self._output[1] * (1.0 - alpha),
            )
        length = math.hypot(output[0], output[1])
        if length < 1e-9:
            output, length = chosen, 1.0
        magnitude = min(max(length, decision.min_magnitude), decision.max_magnitude)
        if abs(magnitude - length) > 1e-9:
            output = (output[0] * magnitude / length, output[1] * magnitude / length)
        self._output = output
        return output


@dataclass(frozen=True)
class _Context:
    pos: Point
    speed: float
    threats: tuple[Threat, ...]
    enemies: tuple[Threat, ...]
    danger_scales: Mapping[str, float]
    samples: tuple[tuple[float, float], ...]
    target: Optional[Point]
    target_weight: float
    band: Band
    arena: Optional[tuple[float, float, float, float]]
    prev_dir: Optional[Point]


# ---- 纯几何辅助 ----


def _player_speed(player: dict, config: ReflexConfig) -> float:
    velocity = point2(player.get("vel")) or (0.0, 0.0)
    return max(math.hypot(velocity[0], velocity[1]), config.player.fallback_speed)


def _nearest_distance(point: Point, threats: tuple[Threat, ...]) -> float:
    if not threats:
        return math.inf
    return min(distance(point, threat.pos) - threat.radius for threat in threats)


def _is_emergency(player: dict, config: ReflexConfig) -> bool:
    hp = player.get("hp")
    max_hp = player.get("max_hp")
    if (
        isinstance(hp, bool)
        or isinstance(max_hp, bool)
        or not isinstance(hp, (int, float))
        or not isinstance(max_hp, (int, float))
        or max_hp <= 0
    ):
        return False
    return hp / max_hp < config.emergency.hp_threshold


def _boundary_cost(
    point: Point, arena: tuple[float, float, float, float], config: ReflexConfig
) -> float:
    margin = config.boundary.margin
    if margin <= 0.0:
        return 0.0
    min_x, min_y, max_x, max_y = arena
    penalty = 0.0
    if point[0] < min_x + margin:
        penalty += ((min_x + margin - point[0]) / margin) ** 2
    elif point[0] > max_x - margin:
        penalty += ((point[0] - (max_x - margin)) / margin) ** 2
    if point[1] < min_y + margin:
        penalty += ((min_y + margin - point[1]) / margin) ** 2
    elif point[1] > max_y - margin:
        penalty += ((point[1] - (max_y - margin)) / margin) ** 2
    return config.boundary.weight * penalty


def _band_cost(point: Point, t: float, context: "_Context", config: ReflexConfig) -> float:
    band_min, band_max = context.band
    nearest = math.inf
    for enemy in context.enemies:
        gap = math.hypot(
            point[0] - (enemy.pos[0] + enemy.vel[0] * t),
            point[1] - (enemy.pos[1] + enemy.vel[1] * t),
        )
        if gap < nearest:
            nearest = gap
    if nearest < band_min:
        return config.kiting.band_weight * (band_min - nearest) / band_min
    if nearest > band_max:
        return config.kiting.far_weight * (nearest - band_max) / band_max
    return 0.0


def _candidates(count: int, prev_dir: Optional[Point]) -> tuple[Point, ...]:
    candidates = []
    for index in range(count):
        angle = 2.0 * math.pi * index / count
        candidates.append((math.cos(angle), math.sin(angle)))
    if prev_dir is not None:
        length = math.hypot(prev_dir[0], prev_dir[1])
        if length > 1e-9:
            previous = (prev_dir[0] / length, prev_dir[1] / length)
            if not any(
                abs(previous[0] - candidate[0]) < 1e-9
                and abs(previous[1] - candidate[1]) < 1e-9
                for candidate in candidates
            ):
                candidates.append(previous)
    return tuple(candidates)


def _angle_between(a: Point, b: Point) -> float:
    dot = a[0] * b[0] + a[1] * b[1]
    cross = a[0] * b[1] - a[1] * b[0]
    return abs(math.atan2(cross, dot))
