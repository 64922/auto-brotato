"""回放战术层指标：驱动战术控制器并统计统一口径（票据 10，纯文件、可复现）。

对录制的每条快照依次驱动战术控制器（``TacticalController``，同时产出 move 与战术状态），
按固定口径统计：

- 采集接近率：有材料可拾取的步中，预测位置（0.3s）比当前位置更接近最近材料的占比；
- 无效移动占比：既不更接近材料、也未改善最近危险间距的步占比（同口径安全定义）；
- 低血保守模式区间：战术状态的 conservative 翻转时间（可观察触发与退出）；
- 录制事实：材料收入/波次曲线（``economy.materials_this_wave`` 的每波峰值）、
  死亡归因（每次 hp 下降最近 3 秒的敌人/弹幕/地雷间距与数量）。

走位口径（0.3s 预测、300px 暴露、60px 贴边、玩家半径 10px）与
``tools.replay.movement_metrics`` 保持一致，保证跨策略/跨票对比可比。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, TYPE_CHECKING

from .movement_metrics import DEBOUNCE_HP, FALLBACK_SPEED, HORIZON_S, PLAYER_RADIUS
from .recording import Recording, iter_records

if TYPE_CHECKING:
    from ab_agent.decision.tactical import TacticalController

#: 判定阈值（评估用，不随策略参数变化）
APPROACH_EPS_PX = 1.0
CLEARANCE_EPS_PX = 1.0
NEAR_RADIUS_PX = 300.0
ATTRIBUTION_WINDOW_S = 3.0


@dataclass(frozen=True)
class TacticalStep:
    """一条快照上的战术控制器输出（vector=None 表示该步未重算，沿用上一步）。"""

    ts: float
    snapshot: dict
    vector: Optional[tuple[float, float]]
    rhythm: Optional[str] = None
    conservative: bool = False
    pickup_window: bool = False
    boss: bool = False
    target_kind: Optional[str] = None


@dataclass(frozen=True)
class ConservativeInterval:
    """低血保守模式的连续区间（秒）。"""

    start_ts: float
    end_ts: float
    start_wave: Optional[int]


@dataclass(frozen=True)
class TacticalMetrics:
    steps: int
    decisions: int
    material_steps: int
    approach_fraction: float
    waste_fraction: float
    conservative_fraction: float
    conservative_intervals: tuple[ConservativeInterval, ...]


@dataclass(frozen=True)
class DamageEvent:
    """一次 hp 下降事件与最近 3 秒的来源归因。"""

    ts: float
    wave: Optional[int]
    amount: float
    nearest_enemy_px: Optional[float]
    nearest_projectile_px: Optional[float]
    nearest_hazard_px: Optional[float]
    enemies_near: int
    projectiles_near: int


@dataclass(frozen=True)
class Outcome:
    """录制的对局结果事实（与策略无关）。"""

    path: Path
    snapshots: int
    start_ts: Optional[float]
    duration_s: float
    waves: tuple[int, ...]
    max_wave: Optional[int]
    materials_by_wave: tuple[tuple[int, int], ...]
    total_materials: int
    death_ts: Optional[float]
    death_wave: Optional[int]


def collect(
    recording: Recording,
    controller: "TacticalController",
    *,
    max_seconds: Optional[float] = None,
) -> list[TacticalStep]:
    """按时间轴把快照喂给战术控制器，收集每步向量与战术状态。"""
    controller.reset()
    steps: list[TacticalStep] = []
    origin: Optional[float] = None
    for record in iter_records(recording):
        if record.kind != "in":
            continue
        envelope = record.data["envelope"]
        if envelope.get("type") != "snapshot":
            continue
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            continue
        if origin is None:
            origin = record.ts
        if max_seconds is not None and record.ts - origin > max_seconds:
            break
        raw = controller.next_move(payload, record.ts)
        vector = None
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            try:
                vector = (float(raw[0]), float(raw[1]))
            except (TypeError, ValueError):
                vector = None
        state = getattr(controller, "state", None)
        steps.append(
            TacticalStep(
                ts=record.ts,
                snapshot=payload,
                vector=vector,
                rhythm=getattr(state, "rhythm", None),
                conservative=bool(getattr(state, "conservative", False)),
                pickup_window=bool(getattr(state, "pickup_window", False)),
                boss=bool(getattr(state, "boss", False)),
                target_kind=getattr(state, "target_kind", None),
            )
        )
    return steps


def evaluate(steps: Sequence[TacticalStep]) -> TacticalMetrics:
    """按统一口径统计采集接近率、无效移动占比与保守模式区间。"""
    held: Optional[tuple[float, float]] = None
    decisions = 0
    material_steps = 0
    approached = 0
    waste = 0
    evaluated = 0
    for step in steps:
        if step.vector is not None:
            held = step.vector
            decisions += 1
        if held is None:
            continue
        point = _predicted_point(step.snapshot, held)
        if point is None:
            continue
        evaluated += 1
        current = _player_point(step.snapshot)
        gap_now = _nearest_material_gap(step.snapshot, current) if current else None
        gap_pred = _nearest_material_gap(step.snapshot, point)
        improved_material = (
            gap_now is not None
            and gap_pred is not None
            and gap_now - gap_pred >= APPROACH_EPS_PX
        )
        if gap_now is not None:
            material_steps += 1
            if improved_material:
                approached += 1
        clearance_now = _clearance(step.snapshot, current) if current else None
        clearance_pred = _clearance(step.snapshot, point)
        improved_safety = (
            clearance_now is not None
            and clearance_pred is not None
            and clearance_pred - clearance_now >= CLEARANCE_EPS_PX
        )
        if not improved_material and not improved_safety:
            waste += 1
    conservative_steps = sum(1 for step in steps if step.conservative)
    return TacticalMetrics(
        steps=len(steps),
        decisions=decisions,
        material_steps=material_steps,
        approach_fraction=(approached / material_steps) if material_steps else 0.0,
        waste_fraction=(waste / evaluated) if evaluated else 0.0,
        conservative_fraction=(conservative_steps / len(steps)) if steps else 0.0,
        conservative_intervals=conservative_intervals(steps),
    )


def conservative_intervals(
    steps: Sequence[TacticalStep],
) -> tuple[ConservativeInterval, ...]:
    """保守模式（conservative=True）的连续区间；退出时刻取最后一秒。"""
    intervals: list[ConservativeInterval] = []
    start_ts: Optional[float] = None
    start_wave: Optional[int] = None
    last_ts: Optional[float] = None
    for step in steps:
        if step.conservative:
            if start_ts is None:
                start_ts = step.ts
                start_wave = _wave_index(step.snapshot)
            last_ts = step.ts
        elif start_ts is not None:
            intervals.append(ConservativeInterval(start_ts, last_ts or start_ts, start_wave))
            start_ts = None
            start_wave = None
            last_ts = None
    if start_ts is not None:
        intervals.append(ConservativeInterval(start_ts, last_ts or start_ts, start_wave))
    return tuple(intervals)


def damage_attribution(
    steps: Sequence[TacticalStep], *, window_s: float = ATTRIBUTION_WINDOW_S
) -> tuple[DamageEvent, ...]:
    """hp 下降事件 + 最近 ``window_s`` 秒内各危险源的最小间距（死亡归因素材）。"""
    events: list[DamageEvent] = []
    previous: Optional[float] = None
    previous_wave: Optional[int] = None
    for index, step in enumerate(steps):
        player = step.snapshot.get("player")
        if not isinstance(player, dict):
            previous, previous_wave = None, None
            continue
        hp = player.get("hp")
        if isinstance(hp, bool) or not isinstance(hp, (int, float)):
            previous, previous_wave = None, None
            continue
        wave = _wave_index(step.snapshot)
        if previous is not None and previous_wave == wave and hp < previous - DEBOUNCE_HP:
            events.append(
                _attribute(steps, index, amount=previous - hp, wave=wave, window_s=window_s)
            )
        previous, previous_wave = float(hp), wave
    return tuple(events)


def materials_curve(steps: Sequence[TacticalStep]) -> tuple[tuple[int, int], ...]:
    """材料收入/波次曲线：每波 ``materials_this_wave`` 的峰值（升序，按波号）。"""
    peaks: dict[int, int] = {}
    for step in steps:
        wave = _wave_index(step.snapshot)
        if wave is None:
            continue
        economy = step.snapshot.get("economy")
        if not isinstance(economy, dict):
            continue
        materials = economy.get("materials_this_wave")
        if isinstance(materials, bool) or not isinstance(materials, int):
            continue
        peaks[wave] = max(peaks.get(wave, 0), materials)
    return tuple(sorted(peaks.items()))


def recording_outcome(recording: Recording) -> Outcome:
    """流式扫描录制，汇总存活波次、材料总量与死亡时刻（与策略无关的事实）。"""
    snapshots = 0
    waves: set[int] = set()
    peaks: dict[int, int] = {}
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    death_ts: Optional[float] = None
    death_wave: Optional[int] = None
    for record in iter_records(recording):
        if record.kind != "in":
            continue
        envelope = record.data["envelope"]
        if envelope.get("type") != "snapshot":
            continue
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            continue
        snapshots += 1
        if first_ts is None:
            first_ts = record.ts
        last_ts = record.ts
        wave = _wave_index(payload)
        if wave is not None:
            waves.add(wave)
        economy = payload.get("economy")
        if wave is not None and isinstance(economy, dict):
            materials = economy.get("materials_this_wave")
            if isinstance(materials, int) and not isinstance(materials, bool):
                peaks[wave] = max(peaks.get(wave, 0), materials)
        player = payload.get("player")
        if (
            death_ts is None
            and isinstance(player, dict)
            and player.get("alive") is False
        ):
            death_ts = record.ts
            death_wave = wave
    materials_by_wave = tuple(sorted(peaks.items()))
    return Outcome(
        path=recording.path,
        snapshots=snapshots,
        start_ts=first_ts,
        duration_s=(last_ts - first_ts) if first_ts is not None and last_ts is not None else 0.0,
        waves=tuple(sorted(waves)),
        max_wave=max(waves) if waves else None,
        materials_by_wave=materials_by_wave,
        total_materials=sum(count for _, count in materials_by_wave),
        death_ts=death_ts,
        death_wave=death_wave,
    )


# ---- 内部辅助（口径与 movement_metrics 一致） ----


def _attribute(
    steps: Sequence[TacticalStep],
    index: int,
    *,
    amount: float,
    wave: Optional[int],
    window_s: float,
) -> DamageEvent:
    event_ts = steps[index].ts
    event_player = _player_point(steps[index].snapshot)
    nearest_enemy = nearest_projectile = nearest_hazard = None
    enemies_near = projectiles_near = 0
    # 60Hz 下 200 步 ≈ 3.3s，覆盖归因窗口；窗口过滤保证低频快照同样正确
    for earlier in steps[max(0, index - 200): index + 1]:
        if event_ts - earlier.ts > window_s:
            continue
        point = _player_point(earlier.snapshot)
        if point is None:
            continue
        gaps = _kind_gaps(earlier.snapshot, point)
        nearest_enemy = _min_opt(nearest_enemy, gaps[0])
        nearest_projectile = _min_opt(nearest_projectile, gaps[1])
        nearest_hazard = _min_opt(nearest_hazard, gaps[2])
    if event_player is not None:
        enemies_near, projectiles_near = _kind_counts(steps[index].snapshot, event_player)
    return DamageEvent(
        ts=event_ts,
        wave=wave,
        amount=amount,
        nearest_enemy_px=nearest_enemy,
        nearest_projectile_px=nearest_projectile,
        nearest_hazard_px=nearest_hazard,
        enemies_near=enemies_near,
        projectiles_near=projectiles_near,
    )


def _kind_gaps(snapshot: dict, point: tuple[float, float]) -> tuple[Optional[float], ...]:
    """玩家位置上各危险源（enemy/projectile/hazard）的最小边缘间距。"""
    best: list[Optional[float]] = [None, None, None]
    for kind_index, key in enumerate(("enemies", "projectiles", "hazards")):
        for item in _sequence(snapshot.get(key)):
            if key == "projectiles" and item.get("friendly"):
                continue
            threat = _threat_position(item)
            if threat is None:
                continue
            x, y, radius = threat
            gap = math.hypot(point[0] - x, point[1] - y) - radius - PLAYER_RADIUS
            if best[kind_index] is None or gap < best[kind_index]:
                best[kind_index] = gap
    return tuple(best)


def _kind_counts(snapshot: dict, point: tuple[float, float]) -> tuple[int, int]:
    enemies = projectiles = 0
    for item in _sequence(snapshot.get("enemies")):
        threat = _threat_position(item)
        if threat is not None and _gap(point, threat) <= NEAR_RADIUS_PX:
            enemies += 1
    for item in _sequence(snapshot.get("projectiles")):
        if item.get("friendly"):
            continue
        threat = _threat_position(item)
        if threat is not None and _gap(point, threat) <= NEAR_RADIUS_PX:
            projectiles += 1
    return enemies, projectiles


def _gap(point: tuple[float, float], threat: tuple[float, float, float]) -> float:
    return math.hypot(point[0] - threat[0], point[1] - threat[1]) - threat[2] - PLAYER_RADIUS


def _nearest_material_gap(
    snapshot: dict, point: Optional[tuple[float, float]]
) -> Optional[float]:
    if point is None:
        return None
    best: Optional[float] = None
    for item in _sequence(snapshot.get("pickups")):
        if item.get("kind") != "material":
            continue
        pos = item.get("pos")
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            continue
        try:
            gap = math.hypot(point[0] - float(pos[0]), point[1] - float(pos[1]))
        except (TypeError, ValueError):
            continue
        best = gap if best is None else min(best, gap)
    return best


def _predicted_point(
    snapshot: dict, vector: tuple[float, float]
) -> Optional[tuple[float, float]]:
    """与 movement_metrics 同口径的 0.3s 满速预测位置。"""
    point = _player_point(snapshot)
    if point is None:
        return None
    length = math.hypot(vector[0], vector[1])
    if length < 1e-9:
        return point
    player = snapshot.get("player")
    speed = FALLBACK_SPEED
    if isinstance(player, dict):
        velocity = player.get("vel")
        if isinstance(velocity, (list, tuple)) and len(velocity) >= 2:
            try:
                speed = max(math.hypot(float(velocity[0]), float(velocity[1])), FALLBACK_SPEED)
            except (TypeError, ValueError):
                pass
    return (
        point[0] + vector[0] / length * speed * HORIZON_S,
        point[1] + vector[1] / length * speed * HORIZON_S,
    )


def _clearance(snapshot: dict, point: tuple[float, float]) -> Optional[float]:
    """预测位置到最近危险源边缘的间距（无危险源 None）。"""
    gaps = _kind_gaps(snapshot, point)
    present = [gap for gap in gaps if gap is not None]
    return min(present) if present else None


def _player_point(snapshot: dict) -> Optional[tuple[float, float]]:
    player = snapshot.get("player")
    if not isinstance(player, dict):
        return None
    pos = player.get("pos")
    if not isinstance(pos, (list, tuple)) or len(pos) < 2:
        return None
    try:
        return (float(pos[0]), float(pos[1]))
    except (TypeError, ValueError):
        return None


def _threat_position(item: dict) -> Optional[tuple[float, float, float]]:
    pos = item.get("pos")
    if not isinstance(pos, (list, tuple)) or len(pos) < 2:
        return None
    try:
        x, y = float(pos[0]), float(pos[1])
    except (TypeError, ValueError):
        return None
    radius = item.get("radius")
    if isinstance(radius, bool) or not isinstance(radius, (int, float)):
        radius = 0.0
    return x, y, max(float(radius), 0.0)


def _wave_index(snapshot: dict) -> Optional[int]:
    wave = snapshot.get("wave")
    if not isinstance(wave, dict):
        return None
    index = wave.get("index")
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    return index


def _sequence(value) -> tuple[dict, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _min_opt(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)
