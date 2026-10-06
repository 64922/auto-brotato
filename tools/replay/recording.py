"""录制文件解析与统计（票据 03，ADR-0008）。

只依赖录制文件本身（NDJSON / NDJSON.gz），不依赖游戏与 agent 包；
大文件按行流式解析，不整体载入内存。

文件结构见 ``agent/ab_agent/recorder.py``：首行 header，其后为 in/out/decision 行。
"""
from __future__ import annotations

import gzip
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional


class RecordingError(ValueError):
    """录制文件结构非法（带行号与原因）。"""


@dataclass(frozen=True)
class Record:
    """一条数据行（不含 header）。"""

    kind: str
    ts: float
    data: dict


@dataclass(frozen=True)
class Recording:
    """已校验 header 的录制文件句柄；数据行经 :func:`iter_records` 流式读取。"""

    path: Path
    header: dict


@dataclass(frozen=True)
class SnapshotStats:
    count: int
    hz: float
    interval_ms_min: Optional[float]
    interval_ms_p50: Optional[float]
    interval_ms_p95: Optional[float]
    interval_ms_max: Optional[float]


@dataclass(frozen=True)
class Summary:
    path: Path
    header: dict
    line_count: int
    in_count: int
    out_count: int
    decision_count: int
    in_by_type: dict = field(default_factory=dict)
    out_by_kind: dict = field(default_factory=dict)
    decision_by_layer: dict = field(default_factory=dict)
    start_ts: Optional[float] = None
    end_ts: Optional[float] = None
    duration: float = 0.0
    wave_min: Optional[int] = None
    wave_max: Optional[int] = None
    snapshot: SnapshotStats = field(
        default_factory=lambda: SnapshotStats(
            count=0,
            hz=0.0,
            interval_ms_min=None,
            interval_ms_p50=None,
            interval_ms_p95=None,
            interval_ms_max=None,
        )
    )


def open_recording(path: str | Path) -> Recording:
    """打开录制文件并校验首行 header；失败抛 :class:`RecordingError`。"""
    path = Path(path)
    try:
        with _open_text(path) as handle:
            first = None
            for line in handle:
                if line.strip():
                    first = line
                    break
    except OSError as exc:
        raise RecordingError("无法打开录制文件：%s" % exc) from exc
    if first is None:
        raise RecordingError("录制文件为空：%s" % path)
    obj = _parse_json(first, 1)
    if obj.get("kind") != "header":
        raise RecordingError("第 1 行应为 header，实际 kind=%r" % obj.get("kind"))
    for key in ("agent_version", "mod_version", "protocol_version", "game_version"):
        if key not in obj:
            raise RecordingError("header 缺少字段 %s" % key)
    return Recording(path=path, header=obj)


def iter_records(recording: Recording) -> Iterator[Record]:
    """流式产出数据行（跳过 header）；结构非法抛 :class:`RecordingError`。"""
    try:
        handle = _open_text(recording.path)
    except OSError as exc:
        raise RecordingError("无法打开录制文件：%s" % exc) from exc
    with handle:
        header_seen = False
        for lineno, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            obj = _parse_json(text, lineno)
            kind = obj.get("kind")
            if not header_seen:
                header_seen = True
                if kind != "header":
                    raise RecordingError("第 %d 行应为 header，实际 kind=%r" % (lineno, kind))
                continue
            if kind == "header":
                raise RecordingError("第 %d 行：重复 header" % lineno)
            if kind not in ("in", "out", "decision"):
                raise RecordingError("第 %d 行：未知 kind=%r" % (lineno, kind))
            ts = obj.get("ts")
            if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                raise RecordingError("第 %d 行：ts 必须是数字" % lineno)
            if kind in ("in", "out"):
                envelope = obj.get("envelope")
                if not isinstance(envelope, dict) or not isinstance(envelope.get("type"), str):
                    raise RecordingError("第 %d 行：envelope 缺失或 type 非法" % lineno)
            else:
                if not isinstance(obj.get("layer"), str) or not isinstance(obj.get("reason"), str):
                    raise RecordingError("第 %d 行：decision 缺少 layer/reason" % lineno)
            yield Record(kind=kind, ts=float(ts), data=obj)


def summarize(recording: Recording) -> Summary:
    """统计消息计数、时长、波次范围与快照频率（单次流式扫描）。"""
    in_count = out_count = decision_count = 0
    in_by_type: Counter = Counter()
    out_by_kind: Counter = Counter()
    decision_by_layer: Counter = Counter()
    snapshot_ts: list[float] = []
    waves: list[int] = []
    start_ts: Optional[float] = None
    end_ts: Optional[float] = None
    for record in iter_records(recording):
        if start_ts is None:
            start_ts = record.ts
        end_ts = record.ts
        if record.kind == "in":
            in_count += 1
            envelope = record.data["envelope"]
            msg_type = envelope["type"]
            in_by_type[msg_type] += 1
            if msg_type == "snapshot":
                snapshot_ts.append(record.ts)
                payload = envelope.get("payload")
                wave = payload.get("wave") if isinstance(payload, dict) else None
                index = wave.get("index") if isinstance(wave, dict) else None
                if isinstance(index, int) and not isinstance(index, bool):
                    waves.append(index)
        elif record.kind == "out":
            out_count += 1
            payload = record.data["envelope"].get("payload")
            kind = payload.get("kind") if isinstance(payload, dict) else None
            out_by_kind[kind if isinstance(kind, str) else "?"] += 1
        else:
            decision_count += 1
            decision_by_layer[record.data["layer"]] += 1

    intervals_ms = [
        (later - earlier) * 1000.0
        for earlier, later in zip(snapshot_ts, snapshot_ts[1:])
        if later > earlier
    ]
    span = snapshot_ts[-1] - snapshot_ts[0] if len(snapshot_ts) >= 2 else 0.0
    hz = (len(snapshot_ts) - 1) / span if span > 0 else 0.0
    stats = SnapshotStats(
        count=len(snapshot_ts),
        hz=hz,
        interval_ms_min=min(intervals_ms) if intervals_ms else None,
        interval_ms_p50=_percentile(intervals_ms, 0.5),
        interval_ms_p95=_percentile(intervals_ms, 0.95),
        interval_ms_max=max(intervals_ms) if intervals_ms else None,
    )
    duration = (end_ts - start_ts) if start_ts is not None and end_ts is not None else 0.0
    return Summary(
        path=recording.path,
        header=recording.header,
        line_count=in_count + out_count + decision_count,
        in_count=in_count,
        out_count=out_count,
        decision_count=decision_count,
        in_by_type=dict(in_by_type),
        out_by_kind=dict(out_by_kind),
        decision_by_layer=dict(decision_by_layer),
        start_ts=start_ts,
        end_ts=end_ts,
        duration=duration,
        wave_min=min(waves) if waves else None,
        wave_max=max(waves) if waves else None,
        snapshot=stats,
    )


def format_summary(summary: Summary) -> str:
    """把统计摘要格式化为多行中文文本（CLI 输出）。"""
    header = summary.header
    lines = [
        "录制文件：%s" % summary.path,
        "header：agent=%s mod=%s protocol=%s game=%s session=%s"
        % (
            header.get("agent_version"),
            header.get("mod_version"),
            header.get("protocol_version"),
            header.get("game_version"),
            header.get("session_id"),
        ),
        "        hero=%s difficulty=%s window=%s"
        % (header.get("hero_id"), header.get("difficulty"), header.get("window")),
        "消息计数：in=%d（snapshot=%d）· out=%d · decision=%d · 共 %d 行"
        % (
            summary.in_count,
            summary.snapshot.count,
            summary.out_count,
            summary.decision_count,
            summary.line_count,
        ),
        "in 分类：%s" % _format_counter(summary.in_by_type),
    ]
    if summary.out_count:
        lines.append("out 动作：%s" % _format_counter(summary.out_by_kind))
    if summary.decision_count:
        lines.append("决策层级：%s" % _format_counter(summary.decision_by_layer))
    lines.append("时长：%.1fs（%s）" % (summary.duration, _format_span(summary)))
    if summary.wave_min is not None:
        lines.append("波次范围：第 %d–%d 波" % (summary.wave_min, summary.wave_max))
    else:
        lines.append("波次范围：无（未进入对局或未记录快照）")
    stats = summary.snapshot
    if stats.interval_ms_p50 is None:
        lines.append("快照频率：%d 条（样本不足，无法统计间隔）" % stats.count)
    else:
        lines.append(
            "快照频率：%d 条 · 平均 %.1fHz · 间隔 min/中位/p95/max = %.1f/%.1f/%.1f/%.1fms"
            % (
                stats.count,
                stats.hz,
                stats.interval_ms_min,
                stats.interval_ms_p50,
                stats.interval_ms_p95,
                stats.interval_ms_max,
            )
        )
    return "\n".join(lines)


def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "rt", encoding="utf-8")


def _parse_json(text: str, lineno: int) -> dict:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RecordingError("第 %d 行：非法 JSON（%s）" % (lineno, exc)) from exc
    if not isinstance(obj, dict):
        raise RecordingError("第 %d 行：必须是 JSON 对象" % lineno)
    return obj


def _percentile(values: list[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _format_counter(counter: dict) -> str:
    if not counter:
        return "无"
    items = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return " · ".join("%s=%s" % (name, count) for name, count in items)


def _format_span(summary: Summary) -> str:
    if summary.start_ts is None:
        return "无数据"
    fmt = "%Y-%m-%d %H:%M:%S"
    return "%s → %s" % (
        time.strftime(fmt, time.localtime(summary.start_ts)),
        time.strftime(fmt, time.localtime(summary.end_ts)),
    )
