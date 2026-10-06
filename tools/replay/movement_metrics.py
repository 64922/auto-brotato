"""回放走位指标：驱动策略并统计统一口径（票据 09，纯文件、可复现）。

对录制的每条快照依次驱动策略，按固定口径统计：

- 危险暴露：对所选方向的预测位置（0.3s 后）计算与危险源的最小间距（clearance），
  汇总平均暴露度（0..1，越高越危险）、暴露步占比（clearance<0）与最小间距；
- 贴边时长：预测位置距场地边界 < 60px 的步占比；
- 方向直方图（8 向）与转向率：相邻步扇区切换次数/秒 + 平均转角（抖振指标）；
- 录制受伤：快照 hp 下降次数/总量（回放既定事实，与策略无关）。

策略无随机性时同一录制 + 同一参数集输出逐字符一致（策略接口见
``ab_agent.move_control.MoveController``）；CLI 与报告格式化见 ``tools.replay.movement``。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence, TYPE_CHECKING

from .recording import Recording, iter_records

if TYPE_CHECKING:
    from ab_agent.move_control import MoveController

#: 指标口径常量（评估用，不随策略参数变化，保证跨参数集可比）
HORIZON_S = 0.3
EDGE_MARGIN = 60.0
EXPOSURE_RANGE = 300.0
FALLBACK_SPEED = 450.0
PLAYER_RADIUS = 10.0
DEBOUNCE_HP = 0.05

#: 扇区标签（屏幕坐标，y 轴向下；index = round(atan2(dy,dx)/(π/4)) mod 8）
_SECTOR_LABELS = ("右E", "右下SE", "下S", "左下SW", "左W", "左上NW", "上N", "右上NE")


@dataclass(frozen=True)
class Step:
    """一条快照上的策略输出（vector=None 表示该步未重算，沿用上一步）。"""

    ts: float
    snapshot: dict
    vector: Optional[tuple[float, float]]


@dataclass(frozen=True)
class Metrics:
    steps: int
    decisions: int
    exposure_mean: float
    exposed_fraction: float
    clearance_min: float
    edge_fraction: float
    mean_magnitude: float
    flips_per_s: float
    mean_turn_deg: float
    sector_counts: tuple[int, ...]


def collect(
    recording: Recording, engine: MoveController, *, max_seconds: Optional[float] = None
) -> list[Step]:
    """按时间轴把快照喂给引擎，收集每步向量（跳过非 snapshot 消息）。"""
    engine.reset()
    steps: list[Step] = []
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
        raw = engine.next_move(payload, record.ts)
        vector = None
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            try:
                vector = (float(raw[0]), float(raw[1]))
            except (TypeError, ValueError):
                vector = None
        steps.append(Step(ts=record.ts, snapshot=payload, vector=vector))
    return steps


def evaluate(steps: Sequence[Step]) -> Metrics:
    """按统一口径统计走位指标；无有效步时返回零值。"""
    held: Optional[tuple[float, float]] = None
    exposures: list[float] = []
    clearances: list[float] = []
    edge_hits = 0
    magnitudes: list[float] = []
    sector_sequence: list[int] = []
    turn_angles: list[float] = []
    flips = 0
    decisions = 0
    previous_decision: Optional[tuple[float, float]] = None
    previous_sector: Optional[int] = None
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    for step in steps:
        if step.vector is not None:
            if previous_decision is not None:
                turn_angles.append(_angle(previous_decision, step.vector))
            previous_decision = step.vector
            held = step.vector
            decisions += 1
        if held is None:
            continue
        first_ts = step.ts if first_ts is None else first_ts
        last_ts = step.ts
        sector = _sector(held)
        sector_sequence.append(sector)
        if previous_sector is not None and sector != previous_sector:
            flips += 1
        previous_sector = sector
        magnitude = math.hypot(held[0], held[1])
        magnitudes.append(magnitude)
        point = predicted_point(step.snapshot, held)
        if point is None:
            continue
        clearance = clearance_at(step.snapshot, point)
        if clearance is not None:
            clearances.append(clearance)
            exposures.append(max(0.0, 1.0 - max(clearance, 0.0) / EXPOSURE_RANGE))
        if _near_edge(step.snapshot, point):
            edge_hits += 1
    span = (last_ts - first_ts) if first_ts is not None and last_ts is not None else 0.0
    exposed = sum(1 for clearance in clearances if clearance < 0.0)
    return Metrics(
        steps=len(steps),
        decisions=decisions,
        exposure_mean=_mean(exposures),
        exposed_fraction=(exposed / len(clearances)) if clearances else 0.0,
        clearance_min=min(clearances) if clearances else 0.0,
        edge_fraction=(edge_hits / len(clearances)) if clearances else 0.0,
        mean_magnitude=_mean(magnitudes),
        flips_per_s=(flips / span) if span > 0 else 0.0,
        mean_turn_deg=_mean(turn_angles),
        sector_counts=tuple(sector_sequence.count(index) for index in range(8)),
    )


def recording_damage(steps: Sequence[Step]) -> tuple[int, float]:
    """快照 hp 下降次数与总量（按波次内比较，忽略跨波次跳变）。"""
    events = 0
    total = 0.0
    previous: Optional[float] = None
    previous_wave: Optional[int] = None
    for step in steps:
        player = step.snapshot.get("player")
        if not isinstance(player, dict):
            previous, previous_wave = None, None
            continue
        hp = player.get("hp")
        if isinstance(hp, bool) or not isinstance(hp, (int, float)):
            previous, previous_wave = None, None
            continue
        wave = step.snapshot.get("wave")
        index = wave.get("index") if isinstance(wave, dict) else None
        if previous is not None and previous_wave == index and hp < previous - DEBOUNCE_HP:
            events += 1
            total += previous - hp
        previous, previous_wave = float(hp), index
    return events, total


def _sector(vector: tuple[float, float]) -> int:
    angle = math.atan2(vector[1], vector[0])
    return int(round(angle / (math.pi / 4.0))) % 8


def _angle(a: tuple[float, float], b: tuple[float, float]) -> float:
    length_a = math.hypot(a[0], a[1])
    length_b = math.hypot(b[0], b[1])
    if length_a < 1e-9 or length_b < 1e-9:
        return 0.0
    dot = (a[0] * b[0] + a[1] * b[1]) / (length_a * length_b)
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def player_point(snapshot: dict) -> Optional[tuple[float, float]]:
    """快照中的玩家位置（非法/缺失返回 None）。"""
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


def predicted_point(
    snapshot: dict, vector: tuple[float, float]
) -> Optional[tuple[float, float]]:
    """0.3s 满速直行预测位置（战术层报告同口径复用）。"""
    point = player_point(snapshot)
    if point is None:
        return None
    px, py = point
    length = math.hypot(vector[0], vector[1])
    if length < 1e-9:
        return (px, py)
    # 方向评估按"满速直行"外推：观测速度更大（击退等）时以其为准
    player = snapshot.get("player")
    velocity = player.get("vel") if isinstance(player, dict) else None
    speed = FALLBACK_SPEED
    if isinstance(velocity, (list, tuple)) and len(velocity) >= 2:
        try:
            observed = math.hypot(float(velocity[0]), float(velocity[1]))
        except (TypeError, ValueError):
            observed = 0.0
        speed = max(observed, FALLBACK_SPEED)
    return (
        px + vector[0] / length * speed * HORIZON_S,
        py + vector[1] / length * speed * HORIZON_S,
    )


def clearance_at(snapshot: dict, point: tuple[float, float]) -> Optional[float]:
    """预测位置到最近危险源边缘的距离（含 0.3s 外推；无危险源返回 None）。

    战术层报告复用本口径（``tactical_metrics``），保证跨策略/跨票据可比。
    """
    best: Optional[float] = None
    for item in sequence(snapshot.get("enemies")) + sequence(snapshot.get("hazards")):
        threat = threat_position(item, HORIZON_S)
        if threat is None:
            continue
        x, y, radius = threat
        distance = math.hypot(point[0] - x, point[1] - y) - radius - PLAYER_RADIUS
        best = distance if best is None else min(best, distance)
    for item in sequence(snapshot.get("projectiles")):
        if item.get("friendly"):
            continue
        horizon = HORIZON_S
        ttl = item.get("ttl")
        if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl > 0.0:
            horizon = min(HORIZON_S, float(ttl))
        threat = threat_position(item, horizon)
        if threat is None:
            continue
        x, y, radius = threat
        distance = math.hypot(point[0] - x, point[1] - y) - radius - PLAYER_RADIUS
        best = distance if best is None else min(best, distance)
    return best


def threat_position(item: dict, t: float) -> Optional[tuple[float, float, float]]:
    """危险源在 ``t`` 秒后的位置与半径（``vel`` 线性外推）。"""
    pos = item.get("pos")
    if not isinstance(pos, (list, tuple)) or len(pos) < 2:
        return None
    try:
        x, y = float(pos[0]), float(pos[1])
    except (TypeError, ValueError):
        return None
    velocity = item.get("vel")
    if isinstance(velocity, (list, tuple)) and len(velocity) >= 2:
        try:
            x += float(velocity[0]) * t
            y += float(velocity[1]) * t
        except (TypeError, ValueError):
            pass
    radius = item.get("radius")
    if isinstance(radius, bool) or not isinstance(radius, (int, float)):
        radius = 0.0
    return x, y, max(float(radius), 0.0)


def _near_edge(snapshot: dict, point: tuple[float, float]) -> bool:
    arena = snapshot.get("arena")
    if not isinstance(arena, dict):
        return False
    low, high = arena.get("min"), arena.get("max")
    if not (
        isinstance(low, (list, tuple))
        and isinstance(high, (list, tuple))
        and len(low) >= 2
        and len(high) >= 2
    ):
        return False
    try:
        return (
            point[0] - float(low[0]) < EDGE_MARGIN
            or float(high[0]) - point[0] < EDGE_MARGIN
            or point[1] - float(low[1]) < EDGE_MARGIN
            or float(high[1]) - point[1] < EDGE_MARGIN
        )
    except (TypeError, ValueError):
        return False


def sequence(value) -> tuple[dict, ...]:
    """取消息中的对象数组（非数组/非对象元素丢弃）。"""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def snapshot_span(steps: Sequence[Step]) -> tuple[list[int], float]:
    """快照覆盖的波次索引（升序）与时长（秒）；movement/tactical 报告共用。"""
    waves = sorted(
        {
            step.snapshot.get("wave", {}).get("index")
            for step in steps
            if isinstance(step.snapshot.get("wave"), dict)
            and isinstance(step.snapshot["wave"].get("index"), int)
        }
    )
    duration = (steps[-1].ts - steps[0].ts) if len(steps) >= 2 else 0.0
    return waves, duration


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
