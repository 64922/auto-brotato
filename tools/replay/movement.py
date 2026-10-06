"""回放走位指标报告（票据 09）：在同一录制上对比走位策略（纯文件、可复现）。

对录制的每条快照依次驱动各策略（反射层 vs 占位基线），按统一口径统计：

- 危险暴露：对所选方向的预测位置（0.3s 后）计算与危险源的最小间距（clearance），
  汇总平均暴露度（0..1，越高越危险）、暴露步占比（clearance<0）与最小间距；
- 贴边时长：预测位置距场地边界 < 60px 的步占比；
- 方向直方图（8 向）与转向率：相邻步扇区切换次数/秒 + 平均转角（抖振指标）；
- 录制受伤：快照 hp 下降次数/总量（回放既定事实，与策略无关）。

反射层无随机；占位基线固定种子（默认 0），同一录制 + 同一参数集输出逐字符一致。
注意：本工具不模拟游戏演进（敌人不因策略改变而改变），只评估"在录制环境里各策略
会选什么方向"，用于参数集对比与抖振回归；实机存活验收见工单 09 验收项。

用法（仓库根目录）：
    python -m tools.replay.movement recordings/xxxx.ndjson
    python -m tools.replay.movement recordings/xxxx.ndjson --config my-reflex.json --json
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Sequence

from .recording import Recording, RecordingError, iter_records, open_recording

#: agent 包路径（tools/replay 从仓库根运行时 `ab_agent` 不在 sys.path）
_AGENT_ROOT = Path(__file__).resolve().parents[2] / "agent"
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from ab_agent.autopilot import PlaceholderAutopilot  # noqa: E402
from ab_agent.decision.config import load_reflex_config  # noqa: E402
from ab_agent.decision.reflex import ReflexController  # noqa: E402

#: 指标口径常量（评估用，不随策略参数变化，保证跨参数集可比）
HORIZON_S = 0.3
EDGE_MARGIN = 60.0
EXPOSURE_RANGE = 300.0
FALLBACK_SPEED = 450.0
PLAYER_RADIUS = 10.0
DEBOUNCE_HP = 0.05

#: 扇区标签（屏幕坐标，y 轴向下；index = round(atan2(dy,dx)/(π/4)) mod 8）
_SECTOR_LABELS = ("右E", "右下SE", "下S", "左下SW", "左W", "左上NW", "上N", "右上NE")


class MoveEngine(Protocol):
    """走位引擎接口（与 ab_agent.run_control.MoveController 一致）。"""

    def reset(self) -> None: ...

    def next_move(self, snapshot: dict, now: float) -> Optional[list[float]]: ...


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


def collect(recording: Recording, engine: MoveEngine, *, max_seconds: Optional[float] = None) -> list[Step]:
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
        point = _predicted_point(step.snapshot, held)
        if point is None:
            continue
        clearance = _clearance(step.snapshot, point)
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


def build_report(
    path: Path,
    runs: Sequence[tuple[str, Sequence[Step]]],
    *,
    note: str,
) -> str:
    """格式化对比报告（中文）。"""
    first = runs[0][1]
    waves = sorted(
        {
            step.snapshot.get("wave", {}).get("index")
            for step in first
            if isinstance(step.snapshot.get("wave"), dict)
            and isinstance(step.snapshot["wave"].get("index"), int)
        }
    )
    duration = (first[-1].ts - first[0].ts) if len(first) >= 2 else 0.0
    lines = [
        "回放走位指标报告（票据 09）",
        "录制：%s" % path,
        "快照：%d 条 · 时长 %.1fs · 波次 %s"
        % (
            len(first),
            duration,
            "第 %d–%d 波" % (waves[0], waves[-1]) if waves else "未知",
        ),
        "口径：预测步长 0.3s · 暴露半径 300px · 贴边边距 60px · 占位种子固定（可复现）",
        note,
        "",
    ]
    metrics = [(name, evaluate(steps)) for name, steps in runs]
    lines.append(_format_table(metrics))
    if len(runs) >= 2:
        lines.append("")
        lines.append(_format_histograms(metrics))
    events, total = recording_damage(first)
    lines.append("")
    lines.append(
        "[录制受伤] %d 次 / 共 %.1f hp（回放既定事实，与策略无关）" % (events, total)
    )
    return "\n".join(lines)


def _format_table(metrics: Sequence[tuple[str, Metrics]]) -> str:
    header = "%-26s %14s %14s %12s" % (
        "指标",
        metrics[0][0],
        metrics[1][0] if len(metrics) > 1 else "",
        "差值",
    )
    lines = ["[策略对比]", header, "-" * len(header)]
    first = metrics[0][1]
    second = metrics[1][1] if len(metrics) > 1 else None

    def row(label: str, a: float, b: Optional[float], fmt: str, delta: str) -> str:
        if b is None:
            return "%-26s %14s" % (label, fmt % a)
        return "%-26s %14s %14s %12s" % (label, fmt % a, fmt % b, delta % (a - b))

    lines.append(row("危险暴露均值(越低越好)", first.exposure_mean, second.exposure_mean if second else None, "%.3f", "%+.3f"))
    lines.append(
        row(
            "暴露步占比(clearance<0)",
            first.exposed_fraction * 100.0,
            (second.exposed_fraction * 100.0) if second else None,
            "%.1f%%",
            "%+.1fpp",
        )
    )
    lines.append(row("最近间距最小值(px)", first.clearance_min, second.clearance_min if second else None, "%.1f", "%+.1f"))
    lines.append(
        row(
            "贴边步占比(<60px)",
            first.edge_fraction * 100.0,
            (second.edge_fraction * 100.0) if second else None,
            "%.1f%%",
            "%+.1fpp",
        )
    )
    lines.append(row("平均指令模长", first.mean_magnitude, second.mean_magnitude if second else None, "%.2f", "%+.2f"))
    lines.append(
        row(
            "方向切换率(次/s, 越低越稳)",
            first.flips_per_s,
            second.flips_per_s if second else None,
            "%.2f",
            "%+.2f",
        )
    )
    lines.append(
        row(
            "决策转角(度, 越低越稳)",
            first.mean_turn_deg,
            second.mean_turn_deg if second else None,
            "%.1f",
            "%+.1f",
        )
    )
    lines.append("决策次数：%s" % " · ".join("%s=%d" % (name, metric.decisions) for name, metric in metrics))
    return "\n".join(lines)


def _format_histograms(metrics: Sequence[tuple[str, Metrics]]) -> str:
    lines = ["[方向直方图（8 向，屏幕坐标 y 向下）]"]
    for name, metric in metrics:
        total = sum(metric.sector_counts)
        parts = []
        for label, count in zip(_SECTOR_LABELS, metric.sector_counts):
            percent = (count / total * 100.0) if total else 0.0
            parts.append("%s %.0f%%" % (label, percent))
        lines.append("%s：%s" % (name, " · ".join(parts)))
    return "\n".join(lines)


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


def _predicted_point(
    snapshot: dict, vector: tuple[float, float]
) -> Optional[tuple[float, float]]:
    player = snapshot.get("player")
    if not isinstance(player, dict):
        return None
    pos = player.get("pos")
    if not isinstance(pos, (list, tuple)) or len(pos) < 2:
        return None
    try:
        px, py = float(pos[0]), float(pos[1])
    except (TypeError, ValueError):
        return None
    length = math.hypot(vector[0], vector[1])
    if length < 1e-9:
        return (px, py)
    # 方向评估按"满速直行"外推：观测速度更大（击退等）时以其为准
    velocity = player.get("vel")
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


def _clearance(snapshot: dict, point: tuple[float, float]) -> Optional[float]:
    """预测位置到最近危险源边缘的距离（含 0.3s 外推；无危险源返回 None）。"""
    best: Optional[float] = None
    for item in _sequence(snapshot.get("enemies")) + _sequence(snapshot.get("hazards")):
        threat = _threat_position(item, HORIZON_S)
        if threat is None:
            continue
        x, y, radius = threat
        distance = math.hypot(point[0] - x, point[1] - y) - radius - PLAYER_RADIUS
        best = distance if best is None else min(best, distance)
    for item in _sequence(snapshot.get("projectiles")):
        if item.get("friendly"):
            continue
        horizon = HORIZON_S
        ttl = item.get("ttl")
        if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl > 0.0:
            horizon = min(HORIZON_S, float(ttl))
        threat = _threat_position(item, horizon)
        if threat is None:
            continue
        x, y, radius = threat
        distance = math.hypot(point[0] - x, point[1] - y) - radius - PLAYER_RADIUS
        best = distance if best is None else min(best, distance)
    return best


def _threat_position(
    item: dict, t: float, default_radius: float = 0.0
) -> Optional[tuple[float, float, float]]:
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
        radius = default_radius
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


def _sequence(value) -> tuple[dict, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def build_engines(config_path: Optional[str], seed: int, baseline: str) -> list[tuple[str, MoveEngine]]:
    """构建对比引擎：反射层（配置）+ 占位基线。"""
    runs: list[tuple[str, MoveEngine]] = [
        ("反射层", ReflexController(load_reflex_config(config_path)))
    ]
    if baseline == "placeholder":
        runs.append(("占位基线", PlaceholderAutopilot(rng=random.Random(seed))))
    return runs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tools.replay.movement", description="回放走位指标报告（票据 09）"
    )
    parser.add_argument("recording", help="录制文件路径（.ndjson 或 .ndjson.gz）")
    parser.add_argument("--config", default=None, help="反射层参数文件（默认包内 config/reflex.json）")
    parser.add_argument(
        "--baseline",
        choices=("placeholder", "none"),
        default="placeholder",
        help="对比基线（默认占位控制器；none=只评估反射层）",
    )
    parser.add_argument("--seed", type=int, default=0, help="占位基线随机种子（默认 0）")
    parser.add_argument("--max-seconds", type=float, default=None, help="只评估前 N 秒（默认全部）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（便于留档对比）")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        recording = open_recording(args.recording)
    except RecordingError as exc:
        print("[错误] %s" % exc, file=sys.stderr)
        return 1
    runs = build_engines(args.config, args.seed, args.baseline)
    collected: list[tuple[str, list[Step]]] = []
    try:
        for name, engine in runs:
            collected.append((name, collect(recording, engine, max_seconds=args.max_seconds)))
    except RecordingError as exc:
        print("[错误] 录制文件损坏：%s" % exc, file=sys.stderr)
        return 1
    note = "反射层参数：%s" % (args.config or "包内默认 config/reflex.json")
    if args.json:
        first_steps = collected[0][1] if collected else []
        waves = sorted(
            {
                step.snapshot.get("wave", {}).get("index")
                for step in first_steps
                if isinstance(step.snapshot.get("wave"), dict)
                and isinstance(step.snapshot["wave"].get("index"), int)
            }
        )
        payload = {
            "recording": str(recording.path),
            "config": args.config,
            "baseline": args.baseline,
            "seed": args.seed,
            "max_seconds": args.max_seconds,
            "snapshots": len(first_steps),
            "duration_s": round(first_steps[-1].ts - first_steps[0].ts, 2)
            if len(first_steps) >= 2
            else 0.0,
            "waves": waves,
            "runs": [
                {"name": name, "metrics": _metrics_dict(evaluate(steps))}
                for name, steps in collected
            ],
            "damage": dict(zip(("events", "total_hp"), recording_damage(collected[0][1]))),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(build_report(recording.path, collected, note=note))
    return 0


def _metrics_dict(metrics: Metrics) -> dict:
    return {
        "steps": metrics.steps,
        "decisions": metrics.decisions,
        "exposure_mean": round(metrics.exposure_mean, 4),
        "exposed_fraction": round(metrics.exposed_fraction, 4),
        "clearance_min": round(metrics.clearance_min, 2),
        "edge_fraction": round(metrics.edge_fraction, 4),
        "mean_magnitude": round(metrics.mean_magnitude, 3),
        "flips_per_s": round(metrics.flips_per_s, 3),
        "mean_turn_deg": round(metrics.mean_turn_deg, 2),
        "sector_counts": list(metrics.sector_counts),
    }


if __name__ == "__main__":
    sys.exit(main())
