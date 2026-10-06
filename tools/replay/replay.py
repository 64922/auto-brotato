"""离线回放 CLI：读取录制文件，按时间轴把 in 消息喂给决策引擎（纯文件）。

用法（conda 环境 brotato，仓库根目录）：
    python -m tools.replay recordings/20261006-153012-unknown-unknown.ndjson --summary
    python -m tools.replay <file> --speed 10          # 10 倍速回放
    python -m tools.replay <file> --speed 0          # 不等待，尽快喂完
    python -m tools.replay <file> --seek 120 --speed 4   # 从第 120 秒开始

--summary 只做统计（消息计数、时长、波次范围、快照频率），不重放。
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Callable, Optional, Sequence

from .engine import DecisionEngine, NullDecisionEngine
from .recording import (
    Recording,
    RecordingError,
    format_summary,
    iter_records,
    open_recording,
    summarize,
)


def replay(
    recording: Recording,
    engine: Optional[DecisionEngine] = None,
    *,
    speed: float = 1.0,
    seek: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """按录制时间轴重放 in 消息；返回喂给引擎的消息数。

    - ``speed``：回放倍速，``0`` 表示不等待（尽快喂完）；默认 1.0；
    - ``seek``：从首条 in 消息起跳过 N 秒后开始；
    - ``sleep``/``clock``：可注入，便于测试。
    """
    if speed < 0:
        raise ValueError("speed 不能为负")
    if seek < 0:
        raise ValueError("seek 不能为负")
    engine = engine or NullDecisionEngine()
    origin: Optional[float] = None
    clock_start = 0.0
    fed = 0
    for record in iter_records(recording):
        if record.kind != "in":
            continue
        if origin is None:
            origin = record.ts + seek
            clock_start = clock()
        if record.ts < origin:
            continue
        if speed > 0:
            delay = (record.ts - origin) / speed - (clock() - clock_start)
            if delay > 0:
                sleep(delay)
        engine.on_message(record.data["envelope"])
        fed += 1
    return fed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tools.replay", description="AutoBrotato 离线回放（票据 03）"
    )
    parser.add_argument("recording", help="录制文件路径（.ndjson 或 .ndjson.gz）")
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="回放倍速，0=不等待（默认 1.0）",
    )
    parser.add_argument(
        "--seek", type=float, default=0.0, help="从首条 in 消息起跳过 N 秒（默认 0）"
    )
    parser.add_argument(
        "--summary", action="store_true", help="只输出统计摘要，不重放"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        recording = open_recording(args.recording)
    except RecordingError as exc:
        print("[错误] %s" % exc, file=sys.stderr)
        return 1
    if args.summary:
        try:
            print(format_summary(summarize(recording)), flush=True)
        except RecordingError as exc:
            print("[错误] %s" % exc, file=sys.stderr)
            return 1
        return 0
    if args.speed < 0 or args.seek < 0:
        print("[错误] --speed/--seek 不能为负", file=sys.stderr)
        return 1
    try:
        fed = replay(recording, speed=args.speed, seek=args.seek)
    except RecordingError as exc:
        print("[错误] 录制文件损坏：%s" % exc, file=sys.stderr)
        return 1
    print(
        "回放完成：%d 条 in 消息已喂给决策引擎（speed=%g，seek=%gs）" % (fed, args.speed, args.seek),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
