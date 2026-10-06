"""agent 命令行入口：启动 IPC 服务端与会话状态机、周期状态打印、命令与难度输入。

用法（conda 环境 brotato）：
    python -m ab_agent.cli
    python -m ab_agent.cli --port 37650 --status-interval 5

终端输入在难度页由 ``RunSession`` 接管：``D0–Dn`` 选难度、``y/n`` 确认模式开关、
``q`` 取消；其余时刻为命令（status/stop/resume/help/quit，``q`` 亦可退出）。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .decision.config import load_reflex_config
from .decision.reflex import ReflexController
from .decision.tactical import TacticalController
from .decision.tactical_config import load_tactical_config
from .ipc_server import DEFAULT_PING_INTERVAL_S, IpcServer
from .recorder import Recorder
from .session import RunSession
from .state import AgentState

LOGGER_NAME = "ab.cli"

#: 默认录制目录：仓库根 ``recordings/``（不入库，见 .gitignore）
DEFAULT_RECORD_DIR = Path(__file__).resolve().parents[2] / "recordings"

#: 全局命令（非难度选择阶段可用；其余输入交给 RunSession 处理）
GLOBAL_COMMANDS = {"status", "stop", "resume", "quit", "exit", "q", "help"}

#: 会话 tick 间隔（秒）：反射层需在 30–60Hz 输出（票据 09），按 60Hz 轮询
SESSION_TICK_INTERVAL_S = 1.0 / 60.0

HELP_TEXT = """命令：
  status   打印当前状态（连接/模式/阶段/快照频率/波次）
  stop     断开连接、让出控制（mod 自动重连后保持 OBSERVE_ONLY）
  resume   恢复接管（清除 OBSERVE_ONLY）
  quit/q   退出（难度选择页的 q 表示取消，不退出）
  help     显示本帮助
难度页：输入 D0–Dn 选择难度；回车重打印清单；q 取消（保持待命）。"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ab_agent",
        description="AutoBrotato agent（IPC 服务端 + 会话状态机，协议 v2）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=37650, help="监听端口（默认 37650）")
    parser.add_argument(
        "--ping-interval",
        type=float,
        default=DEFAULT_PING_INTERVAL_S,
        help="心跳 ping 间隔秒数，0 关闭（默认 1.0）",
    )
    parser.add_argument(
        "--status-interval",
        type=float,
        default=5.0,
        help="周期状态打印间隔秒数，0 关闭（默认 5）",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（默认 INFO）",
    )
    parser.add_argument(
        "--record-dir",
        default=str(DEFAULT_RECORD_DIR),
        help="回放录制目录（默认 <仓库>/recordings）",
    )
    parser.add_argument(
        "--no-record", action="store_true", help="关闭回放录制（默认开启）"
    )
    parser.add_argument(
        "--record-compress", action="store_true", help="录制以 gzip 归档（.ndjson.gz）"
    )
    parser.add_argument(
        "--reflex-config",
        default=None,
        help="反射层参数文件（默认 ab_agent/decision/config/reflex.json；战术层下同样生效）",
    )
    parser.add_argument(
        "--tactical-config",
        default=None,
        help="战术层参数文件（默认 ab_agent/decision/config/tactical.json）",
    )
    parser.add_argument(
        "--move-controller",
        choices=("tactical", "reflex", "placeholder"),
        default="tactical",
        help="战斗走位控制器（默认 tactical=战术层+反射层；reflex/placeholder 为对照基线）",
    )
    parser.add_argument("--version", action="version", version="ab_agent %s" % __version__)
    return parser


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def _print_flush(text: str) -> None:
    """会话输出走带 flush 的 print：stdout 非 tty（管道/重定向）时不吞缓冲。"""
    print(text, flush=True)


def format_status(server: IpcServer, state: AgentState, session: RunSession) -> str:
    """一行状态摘要（status 命令与周期打印共用）。"""
    text = state.describe()
    if session is not None:
        text += " · " + session.describe()
    if server.connected:
        return "[状态] " + text
    return "[状态] " + text + "（等待 mod 连接 %s:%d）" % (
        server.host,
        server.bound_port,
    )


def _pump_stdin(queue: "asyncio.Queue[Optional[str]]", loop: asyncio.AbstractEventLoop) -> None:
    """守护线程读取 stdin，逐行投递到事件循环；EOF 投递 None。

    用守护线程 + 队列而非 asyncio.to_thread：Ctrl+C 时 asyncio.run 会等待
    执行器线程结束，阻塞在 readline 的 to_thread 会导致退出卡住。
    """
    while True:
        try:
            line = sys.stdin.readline()
        except (OSError, ValueError):
            line = ""
        if line == "":
            loop.call_soon_threadsafe(queue.put_nowait, None)
            return
        loop.call_soon_threadsafe(queue.put_nowait, line)


async def _handle_command(
    command: str, server: IpcServer, state: AgentState, session: RunSession
) -> bool:
    """处理一条命令；返回 False 表示退出。"""
    if command in ("quit", "exit", "q"):
        return False
    if command == "status":
        print(format_status(server, state, session), flush=True)
    elif command == "stop":
        state.observe_only = True
        session.on_stop()
        if server.disconnect_client():
            print(
                "已断开连接并让出控制；mod 将自动重连，重连后保持 OBSERVE_ONLY（输入 resume 恢复接管）",
                flush=True,
            )
        else:
            print("当前无活动连接；已保持 OBSERVE_ONLY", flush=True)
    elif command == "resume":
        state.observe_only = False
        session.on_resume()
    elif command == "help":
        print(HELP_TEXT, flush=True)
    elif command:
        print("未知命令：%s（输入 help 查看帮助）" % command, flush=True)
    return True


async def _command_loop(server: IpcServer, state: AgentState, session: RunSession) -> None:
    log = logging.getLogger(LOGGER_NAME)
    loop = asyncio.get_running_loop()
    queue: "asyncio.Queue[Optional[str]]" = asyncio.Queue()
    threading.Thread(
        target=_pump_stdin, args=(queue, loop), name="ab-stdin", daemon=True
    ).start()
    while True:
        sys.stdout.write("agent> ")
        sys.stdout.flush()
        command = await queue.get()
        if command is None:
            log.info("标准输入已关闭，保持运行（Ctrl+C 退出；状态仍会周期打印）")
            await asyncio.Event().wait()
            return
        text = command.strip()
        lowered = text.lower()
        if lowered in GLOBAL_COMMANDS:
            if lowered == "q" and session.handle_input(text):
                continue  # 难度选择中 q 为取消，不退出
            if not await _handle_command(lowered, server, state, session):
                return
            continue
        if session.handle_input(text):
            continue
        if not await _handle_command(text, server, state, session):
            return


async def _status_loop(
    server: IpcServer, state: AgentState, session: RunSession, interval: float
) -> None:
    while True:
        await asyncio.sleep(interval)
        if server.connected or state.session_id is not None:
            print(format_status(server, state, session), flush=True)


async def _session_loop(session: RunSession, interval: float) -> None:
    """周期驱动状态机；单次 tick 异常记录后继续，不拖垮链路。"""
    log = logging.getLogger(LOGGER_NAME)
    while True:
        try:
            await session.tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("会话状态机 tick 异常（已忽略，继续运行）")
        await asyncio.sleep(interval)


async def _run(args: argparse.Namespace) -> int:
    log = logging.getLogger(LOGGER_NAME)
    state = AgentState()
    recorder: Optional[Recorder] = None
    if not args.no_record:
        recorder = Recorder(Path(args.record_dir), compress=args.record_compress)
    server = IpcServer(
        state,
        host=args.host,
        port=args.port,
        ping_interval=args.ping_interval,
        recorder=recorder,
    )
    if args.move_controller == "placeholder":
        from .autopilot import PlaceholderAutopilot

        move_controller = PlaceholderAutopilot()
        log.info("战斗走位使用占位控制器（对照实验；战术层见 --move-controller tactical）")
    elif args.move_controller == "reflex":
        move_controller = ReflexController(load_reflex_config(args.reflex_config))
        log.info("战斗走位使用反射层（对照实验；战术层见 --move-controller tactical）")
    else:
        move_controller = TacticalController(
            load_tactical_config(args.tactical_config),
            reflex=ReflexController(load_reflex_config(args.reflex_config)),
        )
        log.info("战斗走位使用战术层（战术 + 反射；--move-controller 可切对照基线）")
    session = RunSession(
        state,
        server,
        output=_print_flush,
        autopilot=move_controller,
        replay_path=(lambda: str(recorder.recording_path) if recorder and recorder.recording_path else None),
    )
    await server.start()
    log.info(
        "AutoBrotato agent v%s 已启动，监听 %s:%d（协议 v2，等待 mod 连接）",
        __version__,
        args.host,
        server.bound_port,
    )
    if recorder is not None:
        log.info(
            "回放录制已开启：%s（%s；--no-record 可关闭）",
            args.record_dir,
            "gzip 归档" if args.record_compress else "NDJSON",
        )
    log.info("命令：status / stop / resume / quit / q（help 查看帮助；难度页直接输入 D0–Dn）")
    status_task: Optional[asyncio.Task] = None
    if args.status_interval > 0:
        status_task = asyncio.create_task(
            _status_loop(server, state, session, args.status_interval), name="ab-status"
        )
    session_task = asyncio.create_task(
        _session_loop(session, SESSION_TICK_INTERVAL_S), name="ab-session"
    )
    try:
        await _command_loop(server, state, session)
    finally:
        for task in (status_task, session_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await server.shutdown()
        log.info("agent 已退出")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
