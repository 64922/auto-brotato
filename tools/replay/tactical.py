"""回放战术层报告 CLI（票据 10）：在同一录制上对比战术层/反射层/占位，并输出对局事实。

指标口径与评估逻辑见 ``tools.replay.tactical_metrics``；本模块负责构建对比引擎
（战术层 vs 仅反射层 vs 占位基线）、录制事实（材料收入/波次曲线、死亡归因、低血保守
模式区间）以及双录制结果对比（存活波次/材料总量，供实机对照使用）。

用法（仓库根目录）：
    python -m tools.replay.tactical recordings/xxxx.ndjson
    python -m tools.replay.tactical recordings/xxxx.ndjson --baseline reflex --json
    python -m tools.replay.tactical tactical.ndjson --compare-with reflex.ndjson
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Optional, Sequence

from .movement_metrics import Step, evaluate as evaluate_movement, snapshot_span
from .recording import Recording, RecordingError, open_recording
from .recording_facts import (
    DamageEvent,
    Outcome,
    damage_attribution,
    materials_curve,
    recording_outcome,
)
from .tactical_metrics import (
    TacticalMetrics,
    TacticalStep,
    collect,
    conservative_intervals,
    evaluate,
)

#: agent 包路径（tools/replay 从仓库根运行时 `ab_agent` 不在 sys.path）
_AGENT_ROOT = Path(__file__).resolve().parents[2] / "agent"
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from ab_agent.autopilot import PlaceholderAutopilot  # noqa: E402
from ab_agent.decision.config import load_reflex_config  # noqa: E402
from ab_agent.decision.reflex import ReflexController  # noqa: E402
from ab_agent.decision.tactical import TacticalController  # noqa: E402
from ab_agent.decision.tactical_config import load_tactical_config  # noqa: E402
from ab_agent.move_control import MoveController  # noqa: E402


def build_engines(
    tactical_config_path: Optional[str],
    reflex_config_path: Optional[str],
    baseline: str,
    seed: int,
) -> list[tuple[str, MoveController]]:
    """构建对比引擎：战术层（默认）在前，其后为对照基线。"""
    runs: list[tuple[str, TacticalController]] = [
        (
            "战术层",
            TacticalController(
                load_tactical_config(tactical_config_path),
                reflex=ReflexController(load_reflex_config(reflex_config_path)),
            ),
        )
    ]
    if baseline == "reflex":
        runs.append(("仅反射层", ReflexController(load_reflex_config(reflex_config_path))))
    elif baseline == "placeholder":
        runs.append(("占位基线", PlaceholderAutopilot(rng=random.Random(seed))))
    return runs


def to_movement_steps(steps: Sequence[TacticalStep]) -> list[Step]:
    return [Step(ts=step.ts, snapshot=step.snapshot, vector=step.vector) for step in steps]


def build_report(
    path: Path,
    runs: Sequence[tuple[str, Sequence[TacticalStep]]],
    *,
    note: str,
    compare: Optional[tuple[Outcome, Outcome]] = None,
) -> str:
    """格式化战术层报告（中文）。"""
    first = runs[0][1]
    waves, duration = snapshot_span(first)
    lines = [
        "回放战术层报告（票据 10）",
        "录制：%s" % path,
        "快照：%d 条 · 时长 %.1fs · 波次 %s"
        % (
            len(first),
            duration,
            ("第 %d–%d 波" % (waves[0], waves[-1])) if waves else "未知",
        ),
        "口径：预测步长 0.3s · 暴露半径 300px · 贴边边距 60px · 采集接近判定 1px（与 movement 报告可比）",
        note,
        "",
        "[录制事实]",
    ]
    lines.extend(_format_facts(first))
    lines.append("")
    lines.append("[策略对比]（同一录制环境；不模拟战局演进）")
    lines.append(_format_table(runs))
    lines.append("")
    lines.append("[低血保守模式]")
    lines.extend(_format_conservative(runs[0][1]))
    if compare is not None:
        lines.append("")
        lines.append("[双录制结果对比]（实机对照口径；回放不模拟战局）")
        lines.extend(_format_compare(compare))
    return "\n".join(lines)


def _format_facts(steps: Sequence[TacticalStep]) -> list[str]:
    lines: list[str] = []
    curve = materials_curve(steps)
    if curve:
        lines.append(
            "材料/波次曲线：%s（共 %d）"
            % (_format_curve(curve), sum(count for _, count in curve))
        )
    else:
        lines.append("材料/波次曲线：无数据")
    events = damage_attribution(steps)
    total = sum(event.amount for event in events)
    if not events:
        lines.append("死亡归因：未观测到受伤事件（hp 无下降）")
        return lines
    lines.append(
        "死亡归因（hp 下降 %d 次 / 共 %.1fhp；来源为最近 3 秒各危险源最小间距）：" % (len(events), total)
    )
    origin = steps[0].ts if steps else 0.0
    for event in events:
        wave_text = "第 %d 波" % event.wave if event.wave is not None else "未知波次"
        lines.append(
            "  t=%.1fs %s 受伤 %.1fhp · 最近3s：敌人×%d 最近 %s · 弹幕×%d 最近 %s · 地雷最近 %s"
            % (
                event.ts - origin,
                wave_text,
                event.amount,
                event.enemies_near,
                _px(event.nearest_enemy_px),
                event.projectiles_near,
                _px(event.nearest_projectile_px),
                _px(event.nearest_hazard_px),
            )
        )
    return lines


def _format_table(runs: Sequence[tuple[str, Sequence[TacticalStep]]]) -> str:
    names = [name for name, _ in runs]
    metrics = [evaluate(steps) for _, steps in runs]
    movement = [evaluate_movement(to_movement_steps(steps)) for _, steps in runs]
    header = "%-24s %14s %14s %12s" % (
        "指标",
        names[0],
        names[1] if len(names) > 1 else "",
        "差值",
    )
    lines = [header, "-" * len(header)]

    def row(label: str, a: float, b: Optional[float], fmt: str, delta: str) -> str:
        if b is None:
            return "%-24s %14s" % (label, fmt % a)
        return "%-24s %14s %14s %12s" % (label, fmt % a, fmt % b, delta % (a - b))

    lines.append(
        row(
            "采集接近率(越高越好)",
            metrics[0].approach_fraction * 100.0,
            metrics[1].approach_fraction * 100.0 if len(metrics) > 1 else None,
            "%.1f%%",
            "%+.1fpp",
        )
    )
    lines.append(
        row(
            "无效移动占比(越低越好)",
            metrics[0].waste_fraction * 100.0,
            metrics[1].waste_fraction * 100.0 if len(metrics) > 1 else None,
            "%.1f%%",
            "%+.1fpp",
        )
    )
    lines.append(
        row(
            "危险暴露均值(越低越好)",
            movement[0].exposure_mean,
            movement[1].exposure_mean if len(movement) > 1 else None,
            "%.3f",
            "%+.3f",
        )
    )
    lines.append(
        row(
            "暴露步占比(clearance<0)",
            movement[0].exposed_fraction * 100.0,
            movement[1].exposed_fraction * 100.0 if len(movement) > 1 else None,
            "%.1f%%",
            "%+.1fpp",
        )
    )
    lines.append(
        "决策次数：%s" % " · ".join("%s=%d" % (name, metric.decisions) for name, metric in zip(names, metrics))
    )
    return "\n".join(lines)


def _format_conservative(steps: Sequence[TacticalStep]) -> list[str]:
    intervals = conservative_intervals(steps)
    fraction = (
        100.0 * sum(1 for step in steps if step.conservative) / len(steps) if steps else 0.0
    )
    if not intervals:
        return ["战术层：未触发（录制血量未低于保守进入阈值）"]
    origin = steps[0].ts if steps else 0.0
    lines = ["战术层：触发 %d 次（占总快照 %.1f%%）" % (len(intervals), fraction)]
    for interval in intervals:
        wave_text = "第 %d 波" % interval.start_wave if interval.start_wave is not None else "未知波次"
        lines.append(
            "  t=%.1fs → %.1fs（%s 进入）"
            % (interval.start_ts - origin, interval.end_ts - origin, wave_text)
        )
    return lines


def _format_compare(compare: tuple[Outcome, Outcome]) -> list[str]:
    base, other = compare
    lines = [
        "指标                     本录制          对比录制        差值",
        "-" * 62,
        "%-24s %14s %14s %12s"
        % (
            "存活波次(最高)",
            base.max_wave if base.max_wave is not None else "无",
            other.max_wave if other.max_wave is not None else "无",
            _delta_opt(base.max_wave, other.max_wave),
        ),
        "%-24s %14d %14d %12s"
        % ("材料总量", base.total_materials, other.total_materials, "%+d" % (base.total_materials - other.total_materials)),
        "%-24s %14s %14s" % ("死亡时刻", _death_text(base), _death_text(other)),
        "%-24s %14s %14s" % ("录制", base.path.name, other.path.name),
    ]
    lines.append(
        "本录制材料曲线：%s" % _format_curve(base.materials_by_wave)
    )
    lines.append(
        "对比录制材料曲线：%s" % _format_curve(other.materials_by_wave)
    )
    return lines


def _format_curve(curve: Sequence[tuple[int, int]]) -> str:
    if not curve:
        return "无数据"
    return " · ".join("第 %d 波=%d" % (wave, count) for wave, count in curve)


def _death_text(outcome: Outcome) -> str:
    if outcome.death_ts is None:
        return "未观测到死亡"
    wave = ("第 %d 波" % outcome.death_wave) if outcome.death_wave is not None else "未知波次"
    relative = (
        outcome.death_ts - outcome.start_ts if outcome.start_ts is not None else outcome.death_ts
    )
    return "t=%.1fs %s" % (relative, wave)


def _delta_opt(a: Optional[int], b: Optional[int]) -> str:
    if a is None or b is None:
        return "n/a"
    return "%+d" % (a - b)


def _px(value: Optional[float]) -> str:
    return "%.0fpx" % value if value is not None else "无"


def _metrics_dict(metrics: TacticalMetrics) -> dict:
    return {
        "steps": metrics.steps,
        "decisions": metrics.decisions,
        "material_steps": metrics.material_steps,
        "approach_fraction": round(metrics.approach_fraction, 4),
        "waste_fraction": round(metrics.waste_fraction, 4),
        "conservative_fraction": round(metrics.conservative_fraction, 4),
        "conservative_intervals": [
            {
                "start_ts": round(interval.start_ts, 3),
                "end_ts": round(interval.end_ts, 3),
                "start_wave": interval.start_wave,
            }
            for interval in metrics.conservative_intervals
        ],
    }


def _event_dict(event: DamageEvent) -> dict:
    return {
        "ts": round(event.ts, 3),
        "wave": event.wave,
        "amount": round(event.amount, 2),
        "nearest_enemy_px": _round_opt(event.nearest_enemy_px),
        "nearest_projectile_px": _round_opt(event.nearest_projectile_px),
        "nearest_hazard_px": _round_opt(event.nearest_hazard_px),
        "enemies_near": event.enemies_near,
        "projectiles_near": event.projectiles_near,
    }


def _outcome_dict(outcome: Outcome) -> dict:
    return {
        "path": str(outcome.path),
        "snapshots": outcome.snapshots,
        "start_ts": _round_opt(outcome.start_ts),
        "duration_s": round(outcome.duration_s, 2),
        "max_wave": outcome.max_wave,
        "materials_by_wave": [list(item) for item in outcome.materials_by_wave],
        "total_materials": outcome.total_materials,
        "death_ts": _round_opt(outcome.death_ts),
        "death_wave": outcome.death_wave,
    }


def _round_opt(value: Optional[float]) -> Optional[float]:
    return round(value, 2) if value is not None else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tools.replay.tactical", description="回放战术层报告（票据 10）"
    )
    parser.add_argument("recording", help="录制文件路径（.ndjson 或 .ndjson.gz）")
    parser.add_argument("--tactical-config", default=None, help="战术层参数文件（默认包内）")
    parser.add_argument("--reflex-config", default=None, help="反射层参数文件（默认包内）")
    parser.add_argument(
        "--baseline",
        choices=("reflex", "placeholder", "none"),
        default="reflex",
        help="对比基线（默认 reflex=仅反射层；none=只评估战术层）",
    )
    parser.add_argument("--seed", type=int, default=0, help="占位基线随机种子（默认 0）")
    parser.add_argument("--max-seconds", type=float, default=None, help="只评估前 N 秒（默认全部）")
    parser.add_argument(
        "--compare-with", default=None, help="对比第二份录制的结果（存活波次/材料总量）"
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON（便于留档对比）")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        recording = open_recording(args.recording)
    except RecordingError as exc:
        print("[错误] %s" % exc, file=sys.stderr)
        return 1
    compare_recording: Optional[Recording] = None
    if args.compare_with is not None:
        try:
            compare_recording = open_recording(args.compare_with)
        except RecordingError as exc:
            print("[错误] 对比录制无法打开：%s" % exc, file=sys.stderr)
            return 1
    engines = build_engines(
        args.tactical_config, args.reflex_config, args.baseline, args.seed
    )
    collected: list[tuple[str, list[TacticalStep]]] = []
    try:
        for name, engine in engines:
            collected.append((name, collect(recording, engine, max_seconds=args.max_seconds)))
    except RecordingError as exc:
        print("[错误] 录制文件损坏：%s" % exc, file=sys.stderr)
        return 1
    note = "战术参数：%s · 反射参数：%s" % (
        args.tactical_config or "包内默认 config/tactical.json",
        args.reflex_config or "包内默认 config/reflex.json",
    )
    compare: Optional[tuple[Outcome, Outcome]] = None
    if compare_recording is not None:
        compare = (
            recording_outcome(recording),
            recording_outcome(compare_recording),
        )
    if args.json:
        payload = _json_payload(recording, collected, args, compare)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(build_report(recording.path, collected, note=note, compare=compare))
    return 0


def _json_payload(
    recording: Recording,
    collected: Sequence[tuple[str, Sequence[TacticalStep]]],
    args: argparse.Namespace,
    compare: Optional[tuple[Outcome, Outcome]],
) -> dict:
    first_steps = collected[0][1] if collected else []
    waves, duration = snapshot_span(first_steps)
    outcome = recording_outcome(recording)
    payload = {
        "recording": str(recording.path),
        "tactical_config": args.tactical_config,
        "reflex_config": args.reflex_config,
        "baseline": args.baseline,
        "seed": args.seed,
        "max_seconds": args.max_seconds,
        "snapshots": len(first_steps),
        "duration_s": round(duration, 2),
        "waves": waves,
        "materials_by_wave": [list(item) for item in outcome.materials_by_wave],
        "total_materials": outcome.total_materials,
        "damage_events": [_event_dict(event) for event in damage_attribution(first_steps)],
        "outcome": _outcome_dict(outcome),
        "runs": [
            {
                "name": name,
                "metrics": _metrics_dict(evaluate(steps)),
                "movement": _movement_metrics_dict(evaluate_movement(to_movement_steps(steps))),
            }
            for name, steps in collected
        ],
    }
    if compare is not None:
        payload["compare"] = {
            "base": _outcome_dict(compare[0]),
            "other": _outcome_dict(compare[1]),
        }
    return payload


def _movement_metrics_dict(metrics) -> dict:
    return {
        "exposure_mean": round(metrics.exposure_mean, 4),
        "exposed_fraction": round(metrics.exposed_fraction, 4),
        "clearance_min": round(metrics.clearance_min, 2),
        "edge_fraction": round(metrics.edge_fraction, 4),
        "mean_magnitude": round(metrics.mean_magnitude, 3),
        "flips_per_s": round(metrics.flips_per_s, 3),
        "mean_turn_deg": round(metrics.mean_turn_deg, 2),
    }


if __name__ == "__main__":
    sys.exit(main())
