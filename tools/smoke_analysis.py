#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端冒烟的回放分析（票据 08）：纯文件、无副作用，可被工具与测试独立驱动。

解析 agent 录制（NDJSON/NDJSON.gz），逐项校验闭环证据：握手版本、难度页/设置/开局、
第 1–2 波、商店离开、升级选卡、快照频率与关键动作回执，并产出关键事件时间线。
文件结构见 ``agent/ab_agent/recorder.py``；解析复用 ``tools.replay.recording``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .replay.recording import (
    Recording,
    RecordingError,
    Summary,
    format_summary,
    iter_records,
    open_recording,
    summarize,
)

#: 与 agent/ab_agent/protocol.py 保持一致（ADR-0005/0009）
PROTOCOL_VERSION = 2
GAME_VERSION = "1.1.15.4"

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"


@dataclass(frozen=True)
class Check:
    """一条冒烟检查项。"""

    name: str
    status: str
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAIL


@dataclass(frozen=True)
class TimelineEvent:
    ts: float
    label: str


@dataclass(frozen=True)
class BattleSummary:
    """冒烟战报数据（主动停止时由录制推断；终局时反映终局结果）。"""

    result: str  # stopped | defeat | victory
    max_wave: Optional[int]
    duration_s: float
    gold: Optional[int] = None
    weapons: tuple[dict, ...] = ()
    items: tuple[dict, ...] = ()


@dataclass(frozen=True)
class RecordingAnalysis:
    path: Path
    checks: list[Check] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)
    summary_text: str = ""
    battle: Optional[BattleSummary] = None


# ---- 回放分析（纯文件，可在测试中独立驱动）----


@dataclass
class _Trace:
    header: dict = field(default_factory=dict)
    in_types: dict[str, int] = field(default_factory=dict)
    out_kinds: dict[str, int] = field(default_factory=dict)
    out_actions: list[tuple[float, object, str, dict]] = field(default_factory=list)
    acks: dict[object, tuple[bool, str]] = field(default_factory=dict)
    menus: list[tuple[float, dict]] = field(default_factory=list)
    shops: list[tuple[float, dict]] = field(default_factory=list)
    wave_first: dict[int, float] = field(default_factory=dict)
    last_snapshot: Optional[dict] = None
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None


def _collect_trace(recording: Recording) -> _Trace:
    trace = _Trace(header=recording.header)
    for record in iter_records(recording):
        trace.first_ts = record.ts if trace.first_ts is None else trace.first_ts
        trace.last_ts = record.ts
        envelope = record.data.get("envelope") if record.kind in ("in", "out") else None
        if envelope is not None:
            msg_type = envelope.get("type")
            payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
            if record.kind == "in":
                trace.in_types[msg_type] = trace.in_types.get(msg_type, 0) + 1
                if msg_type == "ack":
                    ref = envelope.get("ref")
                    trace.acks[ref] = (payload.get("ok") is True, str(payload.get("error") or ""))
                elif msg_type == "menu":
                    trace.menus.append((record.ts, payload))
                elif msg_type == "shop":
                    trace.shops.append((record.ts, payload))
                elif msg_type == "snapshot":
                    trace.last_snapshot = payload
                    wave = payload.get("wave")
                    index = wave.get("index") if isinstance(wave, dict) else None
                    if (
                        isinstance(index, int)
                        and not isinstance(index, bool)
                        and wave.get("phase") == "combat"
                        and index not in trace.wave_first
                    ):
                        trace.wave_first[index] = record.ts
            else:
                payload_kind = payload.get("kind")
                if isinstance(payload_kind, str):
                    trace.out_kinds[payload_kind] = trace.out_kinds.get(payload_kind, 0) + 1
                    trace.out_actions.append(
                        (record.ts, envelope.get("ref"), payload_kind, payload)
                    )
    return trace


def _action_ok(trace: _Trace, kind: str, *, after: float = 0.0) -> bool:
    """after 之后是否有该动作的成功回执。"""
    for action_ts, ref, action_kind, _payload in trace.out_actions:
        if action_kind != kind or action_ts < after:
            continue
        ack = trace.acks.get(ref)
        if ack is not None and ack[0]:
            return True
    return False


def _first_action(trace: _Trace, kind: str) -> Optional[tuple[float, object, dict]]:
    for ts, ref, action_kind, payload in trace.out_actions:
        if action_kind == kind:
            return ts, ref, payload
    return None


def _first_menu(trace: _Trace, phase: str) -> Optional[float]:
    for ts, payload in trace.menus:
        if payload.get("phase") == phase:
            return ts
    return None


def _first_shop(trace: _Trace, min_wave_next: int) -> Optional[float]:
    for ts, payload in trace.shops:
        wave_next = payload.get("wave_next")
        if isinstance(wave_next, int) and not isinstance(wave_next, bool) and wave_next >= min_wave_next:
            return ts
    return None


def _ack_check(
    trace: _Trace,
    kind: str,
    name: str,
    *,
    require_value: Optional[int] = None,
    value_label: str = "",
) -> Check:
    """校验指定动作已发送且收到成功回执（可选校验发送值）。"""
    action = _first_action(trace, kind)
    if action is None:
        return Check(name, FAIL, "未发送动作")
    _, ref, payload = action
    ack = trace.acks.get(ref)
    if ack is None:
        return Check(name, FAIL, "无回执")
    if not ack[0]:
        return Check(name, FAIL, "回执失败：%s" % ack[1])
    if require_value is not None and payload.get("value") != require_value:
        return Check(name, FAIL, "发送值 %r ≠ 期望 %r" % (payload.get("value"), require_value))
    return Check(name, PASS, value_label)


def analyze_recording(
    path: str | Path,
    *,
    difficulty: int,
    ended_early: bool = False,
) -> RecordingAnalysis:
    """离线校验一次冒烟录制的完整性与闭环证据（票据 08 验收项）。"""
    path = Path(path)
    try:
        recording = open_recording(path)
    except RecordingError as exc:
        return RecordingAnalysis(path=path, checks=[Check("回放文件可解析", FAIL, str(exc))])
    try:
        trace = _collect_trace(recording)
        summary = summarize(recording)
    except RecordingError as exc:
        return RecordingAnalysis(path=path, checks=[Check("回放文件可解析", FAIL, str(exc))])

    checks: list[Check] = []
    timeline: list[tuple[float, str]] = []
    if trace.first_ts is None:
        return RecordingAnalysis(path=path, checks=[Check("回放文件可解析", FAIL, "无数据行")])

    checks.append(
        Check(
            "回放文件可解析",
            PASS,
            "%d 行（in=%d / out=%d）· 时长 %.1fs"
            % (summary.line_count, summary.in_count, summary.out_count, summary.duration),
        )
    )

    header = trace.header
    version_ok = (
        header.get("protocol_version") == PROTOCOL_VERSION
        and header.get("game_version") == GAME_VERSION
        and bool(trace.in_types.get("hello"))
    )
    checks.append(
        Check(
            "握手与版本（协议 v2 / 游戏 %s）" % GAME_VERSION,
            PASS if version_ok else FAIL,
            "protocol=%s game=%s mod=%s session=%s"
            % (
                header.get("protocol_version"),
                header.get("game_version"),
                header.get("mod_version"),
                header.get("session_id"),
            ),
        )
    )

    run_end_ts = _first_menu(trace, "run_end")
    ended_early = ended_early or run_end_ts is not None
    shop3_ts = _first_shop(trace, 3)
    #: 停止点（第 2 波结束的商店或终局）：此后的在途动作属于收尾，不参与回执检查
    stop_ts = shop3_ts if shop3_ts is not None else run_end_ts

    difficulty_menu_ts = _first_menu(trace, "difficulty_select")
    if difficulty_menu_ts is None:
        checks.append(Check("难度页观测（menu=difficulty_select）", FAIL, "未观测到难度页"))
    else:
        timeline.append((difficulty_menu_ts, "难度页观测"))
        checks.append(Check("难度页观测（menu=difficulty_select）", PASS, "已收到难度页 payload"))

    set_action = _first_action(trace, "menu_set_difficulty")
    if set_action is not None:
        timeline.append((set_action[0], "menu_set_difficulty"))
    checks.append(
        _ack_check(
            trace,
            "menu_set_difficulty",
            "难度设置（menu_set_difficulty）",
            require_value=difficulty,
            value_label="D%d 已确认" % difficulty,
        )
    )

    start_action = _first_action(trace, "menu_start_run")
    if start_action is not None:
        timeline.append((start_action[0], "menu_start_run"))
    checks.append(
        _ack_check(trace, "menu_start_run", "自动开局（menu_start_run）", value_label="开局动作已确认")
    )

    wave1_ts = trace.wave_first.get(1)
    if wave1_ts is None:
        status = SKIP if ended_early else FAIL
        checks.append(Check("第 1 波战斗", status, "未观测到第 1 波 combat" + ("（对局提前结束）" if ended_early else "")))
    else:
        timeline.append((wave1_ts, "第 1 波 combat"))
        checks.append(Check("第 1 波战斗", PASS, "已进入 combat"))

    shop2_ts = _first_shop(trace, 2)
    if shop2_ts is None:
        status = SKIP if ended_early else FAIL
        checks.append(Check("第 1 波结束 → 商店自动离开", status, "未观测到下一波=2 的商店" + ("（对局提前结束）" if ended_early else "")))
    else:
        timeline.append((shop2_ts, "商店（下一波=2）"))
        left = _action_ok(trace, "shop_leave", after=shop2_ts)
        if left:
            checks.append(Check("第 1 波结束 → 商店自动离开", PASS, "已离开并进入下一波"))
        else:
            checks.append(Check("第 1 波结束 → 商店自动离开", FAIL, "商店打开但无成功 shop_leave 回执"))

    wave2_ts = trace.wave_first.get(2)
    if wave2_ts is None:
        status = SKIP if ended_early else FAIL
        checks.append(Check("第 2 波战斗", status, "未观测到第 2 波 combat" + ("（对局提前结束）" if ended_early else "")))
    else:
        timeline.append((wave2_ts, "第 2 波 combat"))
        checks.append(Check("第 2 波战斗", PASS, "已进入 combat"))

    if shop3_ts is not None:
        timeline.append((shop3_ts, "商店（下一波=3）"))
        checks.append(Check("第 2 波结束", PASS, "已观测到下一波=3 的商店（第 2 波完成）"))
    elif run_end_ts is not None:
        timeline.append((run_end_ts, "终局（run_end）"))
        checks.append(Check("第 2 波结束", SKIP, "本局提前终局（死亡/胜利）；流程按票据 08 视为通过"))
    elif ended_early:
        checks.append(Check("第 2 波结束", SKIP, "本局提前结束（终端已打印对局战报）；流程按票据 08 视为通过"))
    else:
        checks.append(Check("第 2 波结束", FAIL, "未观测到第 2 波结束（超时或中断）"))

    level_up_ts = _first_menu(trace, "level_up")
    if level_up_ts is None:
        checks.append(Check("升级选卡（出现时）", SKIP, "本局未触发升级页（累计经验不足时不会出现）"))
    else:
        timeline.append((level_up_ts, "升级页（level_up）"))
        level_up_count = sum(1 for _ts, payload in trace.menus if payload.get("phase") == "level_up")
        picks = [a for a in trace.out_actions if a[2] == "menu_pick_upgrade"]
        pick_ok = any(
            (trace.acks.get(ref) or (False, ""))[0] for _ts, ref, _kind, _payload in picks
        )
        if pick_ok:
            checks.append(Check("升级选卡（出现时）", PASS, "%d 次升级推送，选卡成功" % level_up_count))
        else:
            checks.append(Check("升级选卡（出现时）", FAIL, "出现升级页但无成功 menu_pick_upgrade 回执"))

    move_count = trace.out_kinds.get("move", 0)
    checks.append(
        Check(
            "占位走位（move 注入）",
            PASS if move_count >= 10 else FAIL,
            "move 动作 %d 条" % move_count,
        )
    )

    stats = summary.snapshot
    hz_ok = stats.count >= 100 and (
        stats.hz >= 45.0 or (stats.interval_ms_p50 is not None and stats.interval_ms_p50 <= 25.0)
    )
    checks.append(
        Check(
            "快照链路（≥45Hz）",
            PASS if hz_ok else FAIL,
            "%d 条 · 平均 %.1fHz · 间隔中位=%s"
            % (
                stats.count,
                stats.hz,
                "%.1fms" % stats.interval_ms_p50 if stats.interval_ms_p50 is not None else "无",
            ),
        )
    )

    critical = {"menu_set_difficulty", "menu_start_run"}
    retryable = {"menu_pick_upgrade", "shop_leave"}
    critical_failures = []
    retryable_failures = []
    for action_ts, ref, kind, _payload in trace.out_actions:
        if kind not in critical and kind not in retryable:
            continue
        if stop_ts is not None and action_ts > stop_ts:
            continue  # 停止后的在途动作（收尾竞态）不计入回执检查
        ack = trace.acks.get(ref)
        if ack is None:
            target = critical_failures if kind in critical else retryable_failures
            target.append("%s#%s 无回执" % (kind, ref))
        elif not ack[0]:
            (critical_failures if kind in critical else retryable_failures).append(
                "%s: %s" % (kind, ack[1])
            )
    if critical_failures:
        checks.append(Check("关键动作回执", FAIL, "；".join(critical_failures[:5])))
    elif retryable_failures:
        checks.append(Check("关键动作回执", WARN, "重试后成功：%s" % "；".join(retryable_failures[:5])))
    else:
        checks.append(Check("关键动作回执", PASS, "关键动作全部成功"))

    if trace.last_ts is not None:
        timeline.append((trace.last_ts, "最后一条记录"))

    return RecordingAnalysis(
        path=path,
        checks=checks,
        timeline=[TimelineEvent(ts=ts, label=label) for ts, label in sorted(timeline, key=lambda item: item[0])],
        summary_text=format_summary(summary),
        battle=_build_battle_summary(trace, summary, ended_early=ended_early),
    )


def _build_battle_summary(trace: _Trace, summary: Summary, *, ended_early: bool) -> BattleSummary:
    """冒烟战报：终局时反映终局结果，主动停止时由最后快照推断。"""
    max_wave = max(trace.wave_first) if trace.wave_first else None
    gold = None
    weapons: tuple[dict, ...] = ()
    items: tuple[dict, ...] = ()
    snapshot = trace.last_snapshot or {}
    economy = snapshot.get("economy")
    if isinstance(economy, dict) and isinstance(economy.get("gold"), int):
        gold = economy["gold"]
    inventory = snapshot.get("inventory")
    if isinstance(inventory, dict):
        weapons = tuple(w for w in (inventory.get("weapons") or []) if isinstance(w, dict))
        items = tuple(i for i in (inventory.get("items") or []) if isinstance(i, dict))

    run_end_payload = next((p for _ts, p in trace.menus if p.get("phase") == "run_end"), None)
    if run_end_payload is not None and run_end_payload.get("result") in ("victory", "defeat"):
        result = run_end_payload["result"]
    elif ended_early:
        result = "early"
    else:
        result = "stopped"
    return BattleSummary(
        result=result,
        max_wave=max_wave,
        duration_s=summary.duration,
        gold=gold,
        weapons=weapons,
        items=items,
    )


