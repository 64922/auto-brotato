"""回放走位指标报告 CLI（票据 09）：在同一录制上对比走位策略并输出报告。

指标口径与评估逻辑见 ``tools.replay.movement_metrics``；本模块负责构建对比引擎
（反射层 vs 占位基线）、格式化中文报告与 JSON 输出。

用法（仓库根目录）：
    python -m tools.replay.movement recordings/xxxx.ndjson
    python -m tools.replay.movement recordings/xxxx.ndjson --config my-reflex.json --json
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Optional, Sequence

from .movement_metrics import (
    Metrics,
    Step,
    collect,
    evaluate,
    recording_damage,
    snapshot_span,
)
from .recording import RecordingError, open_recording

#: agent 包路径（tools/replay 从仓库根运行时 `ab_agent` 不在 sys.path）
_AGENT_ROOT = Path(__file__).resolve().parents[2] / "agent"
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from ab_agent.autopilot import PlaceholderAutopilot  # noqa: E402
from ab_agent.decision.config import load_reflex_config  # noqa: E402
from ab_agent.decision.reflex import ReflexController  # noqa: E402
from ab_agent.move_control import MoveController  # noqa: E402

_SECTOR_LABELS = ("右E", "右下SE", "下S", "左下SW", "左W", "左上NW", "上N", "右上NE")


def build_report(
    path: Path,
    runs: Sequence[tuple[str, Sequence[Step]]],
    *,
    note: str,
) -> str:
    """格式化对比报告（中文）。"""
    first = runs[0][1]
    waves, duration = snapshot_span(first)
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


def build_engines(config_path: Optional[str], seed: int, baseline: str) -> list[tuple[str, MoveController]]:
    """构建对比引擎：反射层（配置）+ 占位基线。"""
    runs: list[tuple[str, MoveController]] = [
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
        waves, duration = snapshot_span(first_steps)
        payload = {
            "recording": str(recording.path),
            "config": args.config,
            "baseline": args.baseline,
            "seed": args.seed,
            "max_seconds": args.max_seconds,
            "snapshots": len(first_steps),
            "duration_s": round(duration, 2),
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


if __name__ == "__main__":
    sys.exit(main())
