"""战术层控制器：目标选择、波次节奏与血量管理（strategy.md §4，票据 10）。

- 按 ``update.interval_s``（默认 0.25s，4Hz）重算一次战术意图，其余时间把缓存意图交给
  反射层（30–60Hz）执行——战术层只改"目标与权重"，不直接控制移动；
- 目标选择：
  - 材料簇：单链贪心聚合 + 距离衰减 + 危险惩罚（用反射层危险场评估目标位）；
  - 回血消耗品：血量比例低于 ``consumable.hp_ratio`` 时优先最近且相对安全的消耗品；
  - Boss：出现 boss 时维持 ``boss.band`` 射程带，并在环上挑选低危险、远离边界的
    "开阔方向"作为期望位置；
- 波次节奏：早期偏采集（低危险权重、高吸引）→ 中后期偏生存 → 敌人清空窗口集中拾取；
- 血量管理：低血触发保守模式（迟滞进出；抬危险权重、压低吸引、放弃远端材料）；
- 输出：:class:`ab_agent.decision.reflex.TacticalIntent`（期望位置 + 距离带 + 权重向量）；
  无目标（无材料/无 Boss）时保持空意图，交给反射层内置默认目标（场地中心）。

本模块无 IO、无随机：时间由调用方传入；同一快照序列重放结果逐位一致（ADR-0008）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .geometry import Arena, Band, Point, arena_rect, distance, items, point2
from .reflex import ReflexController, TacticalIntent
from .tactical_config import (
    ConservativeConfig,
    RhythmConfig,
    TacticalConfig,
    load_tactical_config,
)

_PICKUP_MATERIAL = "material"
_PICKUP_CONSUMABLE = "consumable"

#: 目标类型标签（报告与决策理由）
TARGET_CLUSTER = "cluster"
TARGET_CONSUMABLE = "consumable"
TARGET_BOSS = "boss"
TARGET_NONE = "none"

_RHYTHM_EARLY = "early"
_RHYTHM_MID = "mid"
_RHYTHM_LATE = "late"


@dataclass(frozen=True)
class TacticalState:
    """一次战术决策的可观测结果（回放报告/调试；不参与控制回路）。"""

    rhythm: str
    pickup_window: bool
    conservative: bool
    boss: bool
    target_kind: str
    target: Optional[Point]
    distance_band: Optional[Band]
    danger_scale: float
    attraction_scale: float
    reason: str


class TacticalController:
    """战术层控制器：``next_move(snapshot, now)`` 返回 move 向量或 None（未到反射重算点）。

    与 ``ReflexController`` 一样满足 :class:`ab_agent.move_control.MoveController`：
    ``reset()`` 清空状态，``next_move`` 由对局编排或回放评估周期调用。
    """

    def __init__(
        self,
        config: Optional[TacticalConfig] = None,
        *,
        reflex: Optional[ReflexController] = None,
    ) -> None:
        self._config = config or load_tactical_config()
        self._reflex = reflex or ReflexController()
        self.state: Optional[TacticalState] = None
        self._intent = TacticalIntent()
        self._last_update_at: Optional[float] = None
        self._conservative = False

    def reset(self) -> None:
        """新对局/停止时清空战术与反射（平滑/节流）状态。"""
        self._reflex.reset()
        self.state = None
        self._intent = TacticalIntent()
        self._last_update_at = None
        self._conservative = False

    @property
    def conservative(self) -> bool:
        """当前是否处于低血保守模式（迟滞；供报告观察触发与退出）。"""
        return self._conservative

    # ---- 对外接口 ----

    def next_move(
        self, snapshot: dict, now: float
    ) -> Optional[list[float]]:
        """到战术重算间隔时更新意图，并把意图交给反射层输出 move 向量。"""
        if not isinstance(snapshot, dict):
            return None
        player = snapshot.get("player")
        if not isinstance(player, dict) or player.get("alive") is False:
            return None
        if (
            self._last_update_at is None
            or now - self._last_update_at >= self._config.update.interval_s
        ):
            self._last_update_at = now
            self.state = self._decide(snapshot, player)
            self._intent = TacticalIntent(
                expected_position=self.state.target,
                distance_band=self.state.distance_band,
                danger_scale=self.state.danger_scale,
                attraction_scale=self.state.attraction_scale,
            )
        return self._reflex.next_move(snapshot, now, intent=self._intent)

    # ---- 决策 ----

    def _decide(self, snapshot: dict, player: dict) -> TacticalState:
        config = self._config
        pos = point2(player.get("pos"))
        hp_ratio = _hp_ratio(player)
        self._conservative = _next_conservative(
            self._conservative, hp_ratio, config.conservative
        )

        arena = arena_rect(snapshot.get("arena"))
        wave_index = _wave_index(snapshot)
        rhythm = _rhythm(wave_index, config.rhythm)
        boss = _boss_position(snapshot)
        pickup_window = (
            boss is None
            and len(items(snapshot.get("enemies"))) <= config.rhythm.clear_enemy_threshold
        )
        if boss is not None:
            danger_scale, attraction_scale = (
                config.boss.danger_scale,
                config.boss.attraction_scale,
            )
        elif pickup_window:
            danger_scale, attraction_scale = (
                config.rhythm.clear_danger_scale,
                config.rhythm.clear_attraction_scale,
            )
        else:
            danger_scale, attraction_scale = _rhythm_scales(rhythm, config.rhythm)

        target: Optional[Point] = None
        target_kind = TARGET_NONE
        distance_band: Optional[Band] = None
        if pos is not None and hp_ratio is not None and hp_ratio < config.consumable.hp_ratio:
            target = _select_consumable(snapshot, pos, self._reflex, config)
            if target is not None:
                target_kind = TARGET_CONSUMABLE
                attraction_scale = max(attraction_scale, config.consumable.attraction_scale)
        if target is None and pos is not None and boss is not None:
            target = _boss_target(snapshot, boss, arena, self._reflex, config)
            if target is not None:
                target_kind = TARGET_BOSS
                distance_band = config.boss.band
        if target is None and pos is not None:
            target = _cluster_target(
                snapshot, pos, self._conservative, self._reflex, config
            )
            if target is not None:
                target_kind = TARGET_CLUSTER

        if self._conservative:
            danger_scale *= config.conservative.danger_scale
            if target_kind != TARGET_CONSUMABLE:
                attraction_scale *= config.conservative.attraction_scale

        return TacticalState(
            rhythm=rhythm,
            pickup_window=pickup_window,
            conservative=self._conservative,
            boss=boss is not None,
            target_kind=target_kind,
            target=target,
            distance_band=distance_band,
            danger_scale=danger_scale,
            attraction_scale=attraction_scale,
            reason=_format_reason(
                rhythm, pickup_window, boss is not None, self._conservative,
                target_kind, target, pos,
            ),
        )


# ---- 目标选择 ----


def _select_consumable(
    snapshot: dict, pos: Point, reflex: ReflexController, config: TacticalConfig
) -> Optional[Point]:
    """低血时挑选消耗品：距离衰减 − 危险惩罚，取最高分（无则 None）。"""
    best: Optional[Point] = None
    best_score = -math.inf
    for pickup in items(snapshot.get("pickups")):
        if pickup.get("kind") != _PICKUP_CONSUMABLE:
            continue
        point = point2(pickup.get("pos"))
        if point is None:
            continue
        gap = distance(pos, point)
        if gap > config.consumable.max_distance:
            continue
        score = _decay(gap, config.cluster.distance_decay) - (
            config.cluster.danger_weight * reflex.danger_at(snapshot, point)
        )
        if score > best_score:
            best, best_score = point, score
    return best


def _cluster_target(
    snapshot: dict,
    pos: Point,
    conservative: bool,
    reflex: ReflexController,
    config: TacticalConfig,
) -> Optional[Point]:
    """材料簇目标：单链贪心聚合 + 距离衰减 + 危险惩罚，返回最高分簇的加权质心。"""
    limit = (
        config.conservative.max_pickup_distance
        if conservative
        else config.cluster.max_distance
    )
    materials: list[tuple[Point, float]] = []
    for pickup in items(snapshot.get("pickups")):
        if pickup.get("kind") != _PICKUP_MATERIAL:
            continue
        point = point2(pickup.get("pos"))
        if point is None:
            continue
        gap = distance(pos, point)
        if gap > limit:
            continue
        materials.append((point, gap))
    if not materials:
        return None
    materials.sort(key=lambda item: (item[1], item[0][0], item[0][1]))

    clusters: list[list[tuple[Point, float]]] = []
    for point, gap in materials:
        for cluster in clusters:
            if any(
                distance(point, member[0]) <= config.cluster.join_radius
                for member in cluster
            ):
                cluster.append((point, gap))
                break
        else:
            clusters.append([(point, gap)])

    scored: list[tuple[float, Point]] = []
    for cluster in clusters:
        weights = [
            _decay(gap, config.cluster.distance_decay) for _, gap in cluster
        ]
        total = sum(weights)
        centroid = (
            sum(point[0] * weight for (point, _), weight in zip(cluster, weights)) / total,
            sum(point[1] * weight for (point, _), weight in zip(cluster, weights)) / total,
        )
        scored.append((total, centroid))
    # 距离衰减和是危险惩罚前的上界：按上界降序评估，上界不超过当前最优即可停止
    #（危险非负），避免高波次下对每个簇各建一次危险场（性能，票据 10）。
    scored.sort(key=lambda item: (-item[0], item[1][0], item[1][1]))
    best: Optional[Point] = None
    best_score = -math.inf
    for upper, centroid in scored:
        if upper <= best_score:
            break
        score = upper - config.cluster.danger_weight * reflex.danger_at(snapshot, centroid)
        if score > best_score:
            best, best_score = centroid, score
    return best


def _boss_target(
    snapshot: dict,
    boss: Point,
    arena: Optional[Arena],
    reflex: ReflexController,
    config: TacticalConfig,
) -> Optional[Point]:
    """Boss 应对：在射程带中径的环上取低危险 + 远离边界的"开阔方向"候选点。"""
    band = config.boss.band
    ring = (band[0] + band[1]) / 2.0
    count = config.boss.candidates
    margin = config.boss.edge_margin
    best: Optional[Point] = None
    best_score = -math.inf
    for index in range(count):
        angle = 2.0 * math.pi * index / count
        point = (boss[0] + math.cos(angle) * ring, boss[1] + math.sin(angle) * ring)
        if arena is not None and not _inside(point, arena):
            continue
        score = -reflex.danger_at(snapshot, point)
        if arena is not None and margin > 0.0:
            edge = _edge_distance(point, arena)
            if edge < margin:
                score -= config.boss.edge_weight * ((margin - edge) / margin) ** 2
        if score > best_score:
            best, best_score = point, score
    return best


# ---- 节奏与血量 ----


def _rhythm(wave_index: Optional[int], config: RhythmConfig) -> str:
    if wave_index is None or wave_index <= config.early_wave_max:
        return _RHYTHM_EARLY
    if wave_index >= config.late_wave_min:
        return _RHYTHM_LATE
    return _RHYTHM_MID


def _rhythm_scales(rhythm: str, config: RhythmConfig) -> tuple[float, float]:
    if rhythm == _RHYTHM_EARLY:
        return config.early_danger_scale, config.early_attraction_scale
    if rhythm == _RHYTHM_LATE:
        return config.late_danger_scale, config.late_attraction_scale
    return config.mid_danger_scale, config.mid_attraction_scale


def _next_conservative(
    current: bool, hp_ratio: Optional[float], config: ConservativeConfig
) -> bool:
    """保守模式迟滞：低于 enter 进入，高于 exit 退出；血量未知保持原状。"""
    if hp_ratio is None:
        return current
    if not current and hp_ratio < config.enter_hp_ratio:
        return True
    if current and hp_ratio >= config.exit_hp_ratio:
        return False
    return current


# ---- 纯辅助 ----


def _hp_ratio(player: dict) -> Optional[float]:
    hp = player.get("hp")
    max_hp = player.get("max_hp")
    if (
        isinstance(hp, bool)
        or isinstance(max_hp, bool)
        or not isinstance(hp, (int, float))
        or not isinstance(max_hp, (int, float))
        or max_hp <= 0
    ):
        return None
    return hp / max_hp


def _wave_index(snapshot: dict) -> Optional[int]:
    wave = snapshot.get("wave")
    if not isinstance(wave, dict):
        return None
    index = wave.get("index")
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    return index


def _boss_position(snapshot: dict) -> Optional[Point]:
    """首个有效 boss（协议保证敌人列表最近优先，故即最近 boss）。"""
    for enemy in items(snapshot.get("enemies")):
        if enemy.get("boss") is True:
            point = point2(enemy.get("pos"))
            if point is not None:
                return point
    return None


def _decay(gap: float, decay_length: float) -> float:
    """距离衰减权重：``decay / (decay + gap)``（越近越高，恒为正）。"""
    return decay_length / (decay_length + max(gap, 0.0))


def _inside(point: Point, arena: Arena) -> bool:
    return arena[0] <= point[0] <= arena[2] and arena[1] <= point[1] <= arena[3]


def _edge_distance(point: Point, arena: Arena) -> float:
    return min(
        point[0] - arena[0],
        arena[2] - point[0],
        point[1] - arena[1],
        arena[3] - point[1],
    )


def _format_reason(
    rhythm: str,
    pickup_window: bool,
    boss: bool,
    conservative: bool,
    target_kind: str,
    target: Optional[Point],
    pos: Optional[Point],
) -> str:
    parts = ["节奏=%s" % rhythm]
    if pickup_window:
        parts.append("清场拾取")
    if boss:
        parts.append("Boss")
    parts.append("保守=%s" % ("开" if conservative else "关"))
    labels = {
        TARGET_CLUSTER: "材料簇",
        TARGET_CONSUMABLE: "回血",
        TARGET_BOSS: "Boss",
        TARGET_NONE: "无",
    }
    target_text = labels.get(target_kind, target_kind)
    if target is not None and pos is not None:
        target_text += "(%.0fpx)" % distance(pos, target)
    parts.append("目标=%s" % target_text)
    return " ".join(parts)
