"""录制事实扫描：材料/波次曲线、死亡归因与对局结果（票据 10，纯文件、可复现）。

这些量与策略无关（录制既定事实），供战术层报告与双录制实机对照：
- 材料收入/波次曲线：每波 ``economy.materials_this_wave`` 的峰值；
- 死亡归因：每次 hp 下降最近 3 秒内敌人/弹幕/地雷的最小间距与受伤时刻 300px 内数量；
- 对局结果：存活波次、材料总量、死亡时刻（供 ``--compare-with`` 对比两局实机录制）。

危险源间距口径与 ``movement_metrics`` 一致（0.3s 外推、弹幕 ttl 截断、玩家半径 10px）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence

from .movement_metrics import (
    DEBOUNCE_HP,
    HORIZON_S,
    PLAYER_RADIUS,
    player_point,
    sequence,
    threat_position,
)
from .recording import Recording, iter_records, wave_index

if TYPE_CHECKING:
    from .tactical_metrics import TacticalStep

#: 归因窗口与「附近」判定（评估口径，不随策略参数变化）
ATTRIBUTION_WINDOW_S = 3.0
NEAR_RADIUS_PX = 300.0


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


def materials_curve(steps: Sequence["TacticalStep"]) -> tuple[tuple[int, int], ...]:
    """材料收入/波次曲线：每波 ``materials_this_wave`` 的峰值（升序，按波号）。"""
    peaks: dict[int, int] = {}
    for step in steps:
        wave = wave_index(step.snapshot)
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


def damage_attribution(
    steps: Sequence["TacticalStep"], *, window_s: float = ATTRIBUTION_WINDOW_S
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
        wave = wave_index(step.snapshot)
        if previous is not None and previous_wave == wave and hp < previous - DEBOUNCE_HP:
            events.append(
                _attribute(steps, index, amount=previous - hp, wave=wave, window_s=window_s)
            )
        previous, previous_wave = float(hp), wave
    return tuple(events)


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
        wave = wave_index(payload)
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


# ---- 内部辅助 ----


def _attribute(
    steps: Sequence["TacticalStep"],
    index: int,
    *,
    amount: float,
    wave: Optional[int],
    window_s: float,
) -> DamageEvent:
    event_ts = steps[index].ts
    event_player = player_point(steps[index].snapshot)
    nearest_enemy = nearest_projectile = nearest_hazard = None
    enemies_near = projectiles_near = 0
    # 60Hz 下 200 步 ≈ 3.3s，覆盖归因窗口；窗口过滤保证低频快照同样正确
    for earlier in steps[max(0, index - 200): index + 1]:
        if event_ts - earlier.ts > window_s:
            continue
        point = player_point(earlier.snapshot)
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
    """玩家位置上各危险源（enemy/projectile/hazard）的最小边缘间距。

    危险源位置含 0.3s 外推、弹幕受 ttl 截断，与 movement 报告的暴露口径一致。
    """
    best: list[Optional[float]] = [None, None, None]
    for kind_index, key in enumerate(("enemies", "projectiles", "hazards")):
        for item in sequence(snapshot.get(key)):
            if key == "projectiles" and item.get("friendly"):
                continue
            horizon = HORIZON_S
            if key == "projectiles":
                ttl = item.get("ttl")
                if (
                    isinstance(ttl, (int, float))
                    and not isinstance(ttl, bool)
                    and ttl > 0.0
                ):
                    horizon = min(HORIZON_S, float(ttl))
            threat = threat_position(item, horizon)
            if threat is None:
                continue
            x, y, radius = threat
            gap = math.hypot(point[0] - x, point[1] - y) - radius - PLAYER_RADIUS
            if best[kind_index] is None or gap < best[kind_index]:
                best[kind_index] = gap
    return tuple(best)


def _kind_counts(snapshot: dict, point: tuple[float, float]) -> tuple[int, int]:
    """受伤时刻 300px 内的敌人数与敌方弹幕数（按当前位置，不外推）。"""
    enemies = projectiles = 0
    for item in sequence(snapshot.get("enemies")):
        threat = threat_position(item, 0.0)
        if threat is not None and _gap(point, threat) <= NEAR_RADIUS_PX:
            enemies += 1
    for item in sequence(snapshot.get("projectiles")):
        if item.get("friendly"):
            continue
        threat = threat_position(item, 0.0)
        if threat is not None and _gap(point, threat) <= NEAR_RADIUS_PX:
            projectiles += 1
    return enemies, projectiles


def _gap(point: tuple[float, float], threat: tuple[float, float, float]) -> float:
    return math.hypot(point[0] - threat[0], point[1] - threat[1]) - threat[2] - PLAYER_RADIUS


def _min_opt(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)
