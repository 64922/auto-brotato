"""回放战术层报告 CLI（票据 10）：在同一录制上对比战术层/反射层/占位，并输出对局事实。

指标口径与评估逻辑见 ``tools.replay.tactical_metrics``，报告文本/JSON 见
``tools.replay.tactical_report``；本模块负责构建对比引擎（战术层 vs 仅反射层 vs 占位
基线）、驱动回放并输出结果（双录制对照按每局汇总，存活波次/材料总量）。

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

from .recording import Recording, RecordingError, open_recording
from .recording_facts import Outcome, recording_runs
from .tactical_metrics import TacticalStep, collect
from .tactical_report import build_report, json_payload

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
    compare: Optional[tuple[tuple[Outcome, ...], tuple[Outcome, ...]]] = None
    if compare_recording is not None:
        compare = (
            recording_runs(recording),
            recording_runs(compare_recording),
        )
    if args.json:
        payload = json_payload(recording, collected, args, compare)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(build_report(recording.path, collected, note=note, compare=compare))
    return 0


if __name__ == "__main__":
    sys.exit(main())
