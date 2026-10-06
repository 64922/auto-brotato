"""回放战术层指标：驱动战术控制器并统计统一口径（票据 10，纯文件、可复现）。

对录制的每条快照依次驱动控制器（战术控制器同时产出 move 与战术状态），按固定口径统计：

- 采集接近率：有材料可拾取的步中，预测位置（0.3s）比当前位置更接近最近材料的占比；
- 无效移动占比：既不更接近材料、也未改善最近危险间距的步占比；
- 低血保守模式区间：战术状态的 conservative 翻转时间（可观察触发与退出）。

走位口径（0.3s 预测、300px 暴露、60px 贴边、玩家半径 10px）复用 ``movement_metrics``，
保证跨策略/跨票对比可比；录制事实（材料/波次曲线、死亡归因、对局结果）见
``tools.replay.recording_facts``。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence, TYPE_CHECKING

from .movement_metrics import clearance_at, player_point, predicted_point, sequence
from .recording import Recording, iter_records, wave_index

if TYPE_CHECKING:
    from ab_agent.move_control import MoveController

#: 判定阈值（评估用，不随策略参数变化）
APPROACH_EPS_PX = 1.0
CLEARANCE_EPS_PX = 1.0


@dataclass(frozen=True)
class TacticalStep:
    """一条快照上的控制器输出（vector=None 表示该步未重算，沿用上一步）。"""

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


def collect(
    recording: Recording,
    controller: "MoveController",
    *,
    max_seconds: Optional[float] = None,
) -> list[TacticalStep]:
    """按时间轴把快照喂给控制器，收集每步向量与战术状态（非战术控制器状态为空）。"""
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
        point = predicted_point(step.snapshot, held)
        if point is None:
            continue
        evaluated += 1
        current = player_point(step.snapshot)
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
        clearance_now = clearance_at(step.snapshot, current) if current else None
        clearance_pred = clearance_at(step.snapshot, point)
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
                start_wave = wave_index(step.snapshot)
            last_ts = step.ts
        elif start_ts is not None:
            intervals.append(ConservativeInterval(start_ts, last_ts or start_ts, start_wave))
            start_ts = None
            start_wave = None
            last_ts = None
    if start_ts is not None:
        intervals.append(ConservativeInterval(start_ts, last_ts or start_ts, start_wave))
    return tuple(intervals)


def _nearest_material_gap(
    snapshot: dict, point: Optional[tuple[float, float]]
) -> Optional[float]:
    if point is None:
        return None
    best: Optional[float] = None
    for item in sequence(snapshot.get("pickups")):
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
