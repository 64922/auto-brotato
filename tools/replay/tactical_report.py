"""回放战术层报告格式化（票据 10）：文本报告与 JSON 载荷（报告 CLI 见 ``tactical``）。

单进程多局时录制按对局切分（``recording_facts.segment_steps``/``recording_runs``），
双录制对照按每局结果汇总（平均/中位），与实机验收口径一致。
"""
from __future__ import annotations

import argparse
import statistics
from pathlib import Path
from typing import Optional, Sequence

from .movement_metrics import Step, evaluate as evaluate_movement, snapshot_span
from .recording import Recording
from .recording_facts import (
    DamageEvent,
    Outcome,
    damage_attribution,
    materials_curve,
    recording_runs,
    segment_steps,
)
from .tactical_metrics import (
    TacticalMetrics,
    TacticalStep,
    conservative_intervals,
    evaluate,
)


def to_movement_steps(steps: Sequence[TacticalStep]) -> list[Step]:
    return [Step(ts=step.ts, snapshot=step.snapshot, vector=step.vector) for step in steps]


def build_report(
    path: Path,
    runs: Sequence[tuple[str, Sequence[TacticalStep]]],
    *,
    note: str,
    compare: Optional[tuple[tuple[Outcome, ...], tuple[Outcome, ...]]] = None,
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
    groups = segment_steps(steps)
    lines = ["对局数：%d" % len(groups)]
    for index, group in enumerate(groups, 1):
        curve = materials_curve(group)
        if curve:
            lines.append(
                "  第 %d 局材料/波次曲线：%s（共 %d）"
                % (index, _format_curve(curve), sum(count for _, count in curve))
            )
        else:
            lines.append("  第 %d 局材料/波次曲线：无数据" % index)
    for index, group in enumerate(groups, 1):
        lines.extend(_format_run_damage(index, group))
    return lines


def _format_run_damage(index: int, steps: Sequence[TacticalStep]) -> list[str]:
    events = damage_attribution(steps)
    if not events:
        return ["  第 %d 局死亡归因：未观测到受伤事件（hp 无下降）" % index]
    total = sum(event.amount for event in events)
    lines = [
        "  第 %d 局死亡归因（hp 下降 %d 次 / 共 %.1fhp；来源为最近 3 秒各危险源最小间距）："
        % (index, len(events), total)
    ]
    origin = steps[0].ts if steps else 0.0
    for event in events:
        wave_text = "第 %d 波" % event.wave if event.wave is not None else "未知波次"
        lines.append(
            "    t=%.1fs %s 受伤 %.1fhp · 最近3s：敌人×%d 最近 %s · 弹幕×%d 最近 %s · 地雷最近 %s"
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


def _format_compare(compare: tuple[tuple[Outcome, ...], tuple[Outcome, ...]]) -> list[str]:
    base_runs, other_runs = compare
    base = _runs_summary(base_runs)
    other = _runs_summary(other_runs)
    lines = [
        "对局数：本录制 %d 局 · 对比录制 %d 局" % (base["runs"], other["runs"]),
        "指标                     本录制(平均/中位)   对比录制(平均/中位)      差值",
        "-" * 66,
        "%-24s %16s %18s %12s"
        % (
            "存活波次(最高)",
            "%.2f / %.1f" % (base["mean_wave"], base["median_wave"]),
            "%.2f / %.1f" % (other["mean_wave"], other["median_wave"]),
            "%+.2f" % (base["mean_wave"] - other["mean_wave"]),
        ),
        "%-24s %16s %18s %12s"
        % (
            "材料总量",
            "%.1f / %.1f" % (base["mean_materials"], base["median_materials"]),
            "%.1f / %.1f" % (other["mean_materials"], other["median_materials"]),
            "%+.1f" % (base["mean_materials"] - other["mean_materials"]),
        ),
        "本录制逐局：%s" % _runs_brief(base_runs),
        "对比录制逐局：%s" % _runs_brief(other_runs),
    ]
    return lines


def _runs_summary(runs: Sequence[Outcome]) -> dict:
    waves = [run.max_wave or 0 for run in runs]
    materials = [run.total_materials for run in runs]
    return {
        "runs": len(runs),
        "mean_wave": statistics.mean(waves) if waves else 0.0,
        "median_wave": statistics.median(waves) if waves else 0.0,
        "mean_materials": statistics.mean(materials) if materials else 0.0,
        "median_materials": statistics.median(materials) if materials else 0.0,
    }


def _runs_brief(runs: Sequence[Outcome]) -> str:
    if not runs:
        return "无对局"
    return " · ".join(
        "第%d局 波%s/材%d"
        % (index, run.max_wave if run.max_wave is not None else "?", run.total_materials)
        for index, run in enumerate(runs, 1)
    )


def _format_curve(curve: Sequence[tuple[int, int]]) -> str:
    if not curve:
        return "无数据"
    return " · ".join("第 %d 波=%d" % (wave, count) for wave, count in curve)


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


def json_payload(
    recording: Recording,
    collected: Sequence[tuple[str, Sequence[TacticalStep]]],
    args: argparse.Namespace,
    compare: Optional[tuple[tuple[Outcome, ...], tuple[Outcome, ...]]],
) -> dict:
    """JSON 载荷（字段与文本报告对应；供留档与批量对比）。"""
    first_steps = collected[0][1] if collected else []
    waves, duration = snapshot_span(first_steps)
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
        "facts": _facts_dicts(recording, first_steps),
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
            "base": _runs_payload(compare[0]),
            "other": _runs_payload(compare[1]),
        }
    return payload


def _facts_dicts(recording: Recording, steps: Sequence[TacticalStep]) -> list[dict]:
    """每局录制事实（与 ``runs`` 的战略指标分离：这里只有既定事实）。"""
    outcomes = recording_runs(recording)
    groups = segment_steps(steps)
    facts: list[dict] = []
    for index, outcome in enumerate(outcomes):
        events = damage_attribution(groups[index]) if index < len(groups) else ()
        facts.append(
            {
                "max_wave": outcome.max_wave,
                "materials_by_wave": [list(item) for item in outcome.materials_by_wave],
                "total_materials": outcome.total_materials,
                "death_ts": _round_opt(outcome.death_ts),
                "death_wave": outcome.death_wave,
                "damage_events": [_event_dict(event) for event in events],
            }
        )
    return facts


def _runs_payload(runs: Sequence[Outcome]) -> dict:
    return {
        "path": str(runs[0].path) if runs else None,
        "runs": [_outcome_dict(run) for run in runs],
        "summary": _runs_summary(runs),
    }


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
