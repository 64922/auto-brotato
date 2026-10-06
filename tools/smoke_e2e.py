#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端闭环冒烟（票据 08）：难度页 → 开局 → 第 1–2 波 → 商店 → 升级 → 停止 → 回放校验。

可重复执行：脚本以子进程方式启动真实 agent（``python -m ab_agent.cli``，含录制），
自动替代"唯一的允许人工输入"（难度值）并全程监控终端输出；到达停止条件
（第 2 波结束，或提前死亡/终局）后发送 ``quit`` 优雅退出，再离线校验回放：

1. 人工前置（不属于脚本职责）：在游戏中选好英雄/初始武器、关闭非常规模式开关，
   停在难度选择页；
2. 脚本检测到难度页提示后自动输入 ``D<n>``，等待读回校验与开局；
3. 自动通过第 1–2 波（agent 占位走位），自动离开商店、自动选升级卡（出现时）；
4. 第 2 波结束（或死亡/终局）→ 停止 agent、刷盘录制并输出冒烟战报；
5. 校验回放可被 ``tools/replay --summary`` 同源解析，并逐项输出冒烟清单结果。

用法（conda 环境 brotato，仓库根目录）：
    python -m tools.smoke_e2e                     # 默认 D0，停在难度页后运行
    python -m tools.smoke_e2e --difficulty D1
    python -m tools.smoke_e2e --record-dir recordings/my-smoke

退出码：0 全部通过；1 存在失败项；130 人工中断。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from .smoke_agent import EOF, SubprocessAgent
from .smoke_analysis import (
    FAIL,
    PASS,
    SKIP,
    BattleSummary,
    Check,
    TimelineEvent,
    analyze_recording,
)

#: agent 包位置（子进程工作目录）
AGENT_ROOT = Path(__file__).resolve().parents[1] / "agent"

#: 默认录制根目录：每次冒烟使用独立时间戳子目录
DEFAULT_RECORD_ROOT = Path(__file__).resolve().parents[1] / "recordings" / "smoke"

#: 冒烟清单（终端前置与流程，供输出与文档共用）
CHECKLIST = (
    "人工前置：游戏中选好英雄/初始武器、关闭无尽/禁用等模式开关，停在难度选择页",
    "难度页检测与提示（agent 打印英雄/武器/难度范围/模式/当前选择）",
    "脚本自动输入难度 → 读回校验 → 自动开局",
    "自动通过第 1–2 波（占位走位，无人工操作）",
    "商店打开：自动评估并离开，成功进入下一波",
    "升级页出现时自动选卡",
    "第 2 波结束（或死亡/终局）→ 停止并输出冒烟战报 → 回放文件完整",
    "回放可被 tools/replay --summary 解析，关键动作回执全部成功",
)

#: 停止前要求完成的波数（票据 08 固定为 2）
STOP_WAVE = 2

# ---- 终端输出标记（agent session/recorder 文案）----

MARK_DIFFICULTY_PROMPT = "检测到难度选择页"
MARK_INPUT_PROMPT = "请输入难度"
MARK_MODES_LINE = re.compile(r"模式开关：(.+)")
MARK_READBACK_OK = "难度读回校验通过"
MARK_RUN_ENTERED = "已进入第 1 波"
MARK_SHOP_SUMMARY = re.compile(r"\[商店\] 下一波=(\d+)")
MARK_REPORT = "对局战报"
MARK_DEATH_FALLBACK = "死亡兜底"
MARK_RECORDING = re.compile(r"开始录制[：:]\s*(.+?)\s*$")

#: 开局序列的失败标记（出现即以失败收尾，不重复输入；须避免匹配常规日志）
START_FAILURES = ("读回校验失败", "开始对局失败", "内部异常", "已取消", "观测到第 1 波战斗")


@dataclass
class SmokeResult:
    passed: bool
    flow_checks: list[Check]
    recording_checks: list[Check]
    timeline: list[TimelineEvent]
    recording_path: Optional[Path]
    summary_text: str = ""
    battle: Optional[BattleSummary] = None

    @property
    def checks(self) -> list[Check]:
        return self.flow_checks + self.recording_checks


@dataclass
class _FlowState:
    """终端输出中采集到的流程状态。"""

    prompted: bool = False
    input_prompt: bool = False
    modes_line: Optional[str] = None
    readback: bool = False
    entered: bool = False
    report: bool = False
    death_fallback: bool = False
    start_failure: Optional[str] = None
    recording: Optional[str] = None
    shop_next: set[int] = field(default_factory=set)


# ---- 冒烟编排 ----


def run_smoke(
    agent,
    *,
    difficulty: int,
    record_root: Path,
    menu_timeout: float = 300.0,
    start_timeout: float = 30.0,
    run_timeout: float = 600.0,
    close_timeout: float = 20.0,
    output: Callable[[str], None] = print,
) -> SmokeResult:
    """驱动 agent 完成闭环并校验回放（agent 可为 SubprocessAgent 或测试 Fake）。"""
    flow: list[Check] = []
    record_root = Path(record_root)
    record_root.mkdir(parents=True, exist_ok=True)
    state = _FlowState()

    def handle(line: str) -> None:
        match = MARK_RECORDING.search(line)
        if match:
            state.recording = match.group(1)
        if MARK_DIFFICULTY_PROMPT in line:
            state.prompted = True
        if MARK_INPUT_PROMPT in line:
            state.input_prompt = True
        match = MARK_MODES_LINE.search(line)
        if match:
            state.modes_line = match.group(1).strip()
        if MARK_READBACK_OK in line:
            state.readback = True
        if MARK_RUN_ENTERED in line:
            state.entered = True
        match = MARK_SHOP_SUMMARY.search(line)
        if match:
            state.shop_next.add(int(match.group(1)))
        if MARK_REPORT in line:
            state.report = True
        if MARK_DEATH_FALLBACK in line:
            state.death_fallback = True
        if state.start_failure is None and not state.entered:
            for marker in START_FAILURES:
                if marker in line:
                    state.start_failure = line.strip()
                    break

    def pump(deadline: float) -> bool:
        """读取一行；返回 False 表示进程输出结束或超时。"""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        line = agent.read_line(min(remaining, 1.0))
        if line is EOF:
            return False
        if line is None:
            return time.monotonic() < deadline
        text = str(line)
        output("  | " + text)
        handle(text)
        return True

    # 1) 等待人工前置完成：难度页提示（多行 prompt 以「请输入难度」收尾）
    deadline = time.monotonic() + menu_timeout
    while not state.input_prompt and state.start_failure is None:
        if not pump(deadline):
            break
    flow.append(
        Check(
            "难度页检测与提示",
            PASS if state.prompted else FAIL,
            "模式开关：%s" % state.modes_line if state.modes_line else "未读取到模式开关行",
        )
    )

    modes_on = bool(state.modes_line and "=开" in state.modes_line)
    mode_detail = state.modes_line or "未读取到"
    if modes_on:
        flow.append(
            Check(
                "模式开关检查",
                FAIL,
                "检测到开启（%s）：请在游戏内关闭无尽/禁用等开关后重跑"
                "（已知限制：无尽模式原生警告弹窗会挡住自动开局，票据 07 限制④）" % mode_detail,
            )
        )
    else:
        flow.append(Check("模式开关检查", PASS, mode_detail))

    # 2) 自动输入难度 → 开局
    if state.prompted and not modes_on and state.start_failure is None:
        agent.send_line("D%d" % difficulty)
        flow.append(Check("难度输入（脚本自动）", PASS, "已输入 D%d（替代唯一允许的人工输入）" % difficulty))
        deadline = time.monotonic() + start_timeout
        while not state.entered and state.start_failure is None:
            if not pump(deadline):
                break
        if state.entered:
            flow.append(
                Check(
                    "开局序列（读回校验 → 进入对局）",
                    PASS,
                    "读回=%s；已进入第 1 波" % ("通过" if state.readback else "未观测（仍进入对局）"),
                )
            )
        else:
            flow.append(
                Check(
                    "开局序列（读回校验 → 进入对局）",
                    FAIL,
                    state.start_failure or "等待开局超时",
                )
            )

    # 3) 等待第 2 波结束（或死亡/终局）
    if state.entered:
        deadline = time.monotonic() + run_timeout
        while state.start_failure is None and not state.report:
            if max(state.shop_next, default=0) > STOP_WAVE:
                break
            if not pump(deadline):
                break
        if max(state.shop_next, default=0) > STOP_WAVE:
            flow.append(
                Check(
                    "到达停止条件（第 %d 波结束）" % STOP_WAVE,
                    PASS,
                    "已观测到下一波=%d 的商店" % (STOP_WAVE + 1),
                )
            )
        elif state.report:
            tail_deadline = time.monotonic() + 1.0
            while pump(tail_deadline):
                if state.death_fallback:
                    break
            flow.append(
                Check(
                    "终局/死亡检测",
                    PASS,
                    "终端已打印对局战报（%s）" % ("死亡兜底" if state.death_fallback else "run_end"),
                )
            )
        else:
            flow.append(
                Check(
                    "到达停止条件（第 %d 波结束）" % STOP_WAVE,
                    FAIL,
                    state.start_failure or "等待第 %d 波结束超时（%.0fs）" % (STOP_WAVE, run_timeout),
                )
            )
    else:
        flow.append(Check("终局/死亡检测", SKIP, "未进入对局"))

    # 4) 优雅停止并落盘
    exit_code = agent.close(timeout=close_timeout)
    if exit_code == 0:
        flow.append(Check("agent 优雅退出（录制已刷盘）", PASS, "exit=0"))
    elif exit_code is None:
        flow.append(Check("agent 优雅退出（录制已刷盘）", FAIL, "超时被强杀，录制可能不完整"))
    else:
        flow.append(Check("agent 优雅退出（录制已刷盘）", FAIL, "exit=%s" % exit_code))

    # 5) 定位录制文件并离线校验
    recording_path: Optional[Path] = None
    if state.recording:
        candidate = Path(state.recording)
        if candidate.exists():
            recording_path = candidate
    if recording_path is None:
        candidates = sorted(record_root.glob("*.ndjson"), key=lambda path: path.stat().st_mtime)
        if candidates:
            recording_path = candidates[-1]

    battle: Optional[BattleSummary] = None
    if recording_path is None:
        recording_checks = [Check("回放文件产出", FAIL, "未在 %s 找到录制文件" % record_root)]
        timeline: list[TimelineEvent] = []
        summary_text = ""
    else:
        analysis = analyze_recording(
            recording_path, difficulty=difficulty, ended_early=bool(state.report)
        )
        recording_checks = analysis.checks
        timeline = analysis.timeline
        summary_text = analysis.summary_text
        battle = analysis.battle

    passed = not any(check.failed for check in flow + recording_checks)
    return SmokeResult(
        passed=passed,
        flow_checks=flow,
        recording_checks=recording_checks,
        timeline=timeline,
        recording_path=recording_path,
        summary_text=summary_text,
        battle=battle,
    )


def format_timeline(events: Sequence[TimelineEvent]) -> list[str]:
    if not events:
        return ["（无）"]
    base = events[0].ts
    return ["  +%7.1fs  %s" % (event.ts - base, event.label) for event in events]


#: 冒烟战报的结果文案（主动停止 / 终局 / 提前结束）
BATTLE_RESULT_LABELS = {
    "stopped": "主动停止（完成第 %d 波）" % STOP_WAVE,
    "victory": "胜利",
    "defeat": "战败（死亡/终局）",
    "early": "提前结束（死亡兜底）",
}


def format_battle(battle: Optional[BattleSummary]) -> list[str]:
    if battle is None:
        return ["  （无回放文件，战报不可用）"]
    weapons = "、".join(
        "%s(T%s)" % (weapon.get("id", "?"), weapon.get("tier", "?")) for weapon in battle.weapons
    )
    items = "、".join(
        "%s×%s" % (item.get("id", "?"), item.get("count", 1)) for item in battle.items
    )
    return [
        "  结果：%s" % BATTLE_RESULT_LABELS.get(battle.result, battle.result),
        "  波次：%s" % ("最远第 %d 波" % battle.max_wave if battle.max_wave else "未进入对局"),
        "  时长：%.1fs" % battle.duration_s,
        "  金币：%s" % (battle.gold if battle.gold is not None else "未知"),
        "  构建：武器 %s / 道具 %s" % (weapons or "无", items or "无"),
    ]


def format_result(result: SmokeResult) -> str:
    lines = ["", "========== 端到端冒烟报告（票据 08） =========="]
    lines.append("[流程检查 · agent 终端]")
    for check in result.flow_checks:
        lines.append("  [%s] %s：%s" % (check.status, check.name, check.detail))
    lines.append("[回放检查 · 录制文件]")
    for check in result.recording_checks:
        lines.append("  [%s] %s：%s" % (check.status, check.name, check.detail))
    lines.append("[冒烟战报]")
    lines.extend(format_battle(result.battle))
    lines.append("[时间线]")
    lines.extend(format_timeline(result.timeline))
    lines.append("回放文件：%s" % (result.recording_path or "未产出"))
    if result.summary_text:
        lines.append("回放摘要：")
        lines.extend("  " + line for line in result.summary_text.splitlines())
    lines.append("结论：%s" % ("PASS（清单全项通过）" if result.passed else "FAIL（存在未通过项）"))
    lines.append("==============================================")
    return "\n".join(lines)


# ---- CLI ----


def parse_difficulty(text: str) -> int:
    match = re.fullmatch(r"[dD]?(\d+)", text.strip())
    if match is None or not 0 <= int(match.group(1)) <= 6:
        raise argparse.ArgumentTypeError("难度需为 D0–D6 或 0–6，得到 %r" % text)
    return int(match.group(1))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tools.smoke_e2e",
        description="AutoBrotato 端到端闭环冒烟（票据 08）：难度页 → 第 2 波 → 回放校验",
    )
    parser.add_argument(
        "--difficulty",
        type=parse_difficulty,
        default=parse_difficulty("D0"),
        help="自动输入的难度（默认 D0）",
    )
    parser.add_argument(
        "--record-dir",
        default=None,
        help="录制目录（默认 <仓库>/recordings/smoke/<时间戳>）",
    )
    parser.add_argument("--port", type=int, default=37650, help="agent 监听端口（默认 37650）")
    parser.add_argument("--python", default=sys.executable, help="运行 agent 的 Python（默认当前解释器）")
    parser.add_argument("--menu-timeout", type=float, default=300.0, help="等待人工停在难度页的秒数（默认 300）")
    parser.add_argument("--start-timeout", type=float, default=30.0, help="等待开局完成的秒数（默认 30）")
    parser.add_argument("--run-timeout", type=float, default=600.0, help="等待第 2 波结束的秒数（默认 600）")
    parser.add_argument("--close-timeout", type=float, default=20.0, help="quit 后等待进程退出的秒数（默认 20）")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    record_root = Path(args.record_dir) if args.record_dir else DEFAULT_RECORD_ROOT / stamp

    print("========== AutoBrotato 端到端冒烟（票据 08） ==========")
    print("前置：人工在游戏中选好英雄/初始武器、关闭非常规模式开关，停在难度选择页。")
    print("脚本将自动输入 D%d、经过第 1–%d 波并在第 %d 波结束后停止。" % (args.difficulty, STOP_WAVE, STOP_WAVE))
    print("清单：")
    for index, item in enumerate(CHECKLIST, start=1):
        print("  %d. %s" % (index, item))
    print("录制目录：%s" % record_root)
    print("等待 agent 检测到难度页（最长 %.0fs）…" % args.menu_timeout)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    agent_argv = [
        args.python,
        "-u",
        "-m",
        "ab_agent.cli",
        "--port",
        str(args.port),
        "--record-dir",
        str(record_root),
    ]
    agent = SubprocessAgent(agent_argv, cwd=AGENT_ROOT, env=env)
    try:
        result = run_smoke(
            agent,
            difficulty=args.difficulty,
            record_root=record_root,
            menu_timeout=args.menu_timeout,
            start_timeout=args.start_timeout,
            run_timeout=args.run_timeout,
            close_timeout=args.close_timeout,
        )
    except KeyboardInterrupt:
        agent.close()
        print("\n[中断] 已停止 agent；冒烟未完成", file=sys.stderr)
        return 130
    print(format_result(result))
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
