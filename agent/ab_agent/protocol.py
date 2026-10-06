"""IPC 协议 v1 纯函数编解码（docs/protocol.md §1–§3）。

信封：``{"v": int, "seq": int, "ts": float, "type": str, "ref": any, "payload": object}``

本模块只做解析/编码/握手校验，不涉及 IO，便于单元测试；asyncio 服务端见 ipc_server.py。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

#: 当前协议版本（mod 0.2.0 为 v1；v2 演进见 ADR-0009，票据 05/06 实施）
PROTOCOL_VERSION = 1

#: 锁定游戏版本（ADR-0005）
GAME_VERSION = "1.1.15.4"

#: welcome 下发的默认配置（protocol.md §2.2）
DEFAULT_WELCOME_CONFIG = {
    "snapshot_hz": 60,
    "debug_overlay": False,
    "action_ttl_ms": 250,
}


class ProtocolError(ValueError):
    """无法解析的协议行/信封。"""


@dataclass(frozen=True)
class Envelope:
    """解析后的协议信封。"""

    v: int
    seq: int
    ts: float
    type: str
    ref: Any
    payload: dict


def parse_line(line: str) -> Envelope:
    """解析一行 NDJSON 信封；失败抛 ProtocolError（调用方丢弃该行）。"""
    text = line.strip()
    if not text:
        raise ProtocolError("空行")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("非法 JSON：%s" % exc) from exc
    if not isinstance(raw, dict):
        raise ProtocolError("信封必须是 JSON 对象")
    msg_type = raw.get("type")
    if not isinstance(msg_type, str) or not msg_type:
        raise ProtocolError("缺少字符串 type 字段")
    payload = raw.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ProtocolError("payload 必须是 JSON 对象")
    return Envelope(
        v=_as_int(raw.get("v"), "v"),
        seq=_as_int(raw.get("seq"), "seq"),
        ts=_as_float(raw.get("ts"), "ts"),
        type=msg_type,
        ref=raw.get("ref"),
        payload=payload,
    )


def encode(
    msg_type: str,
    payload: dict | None = None,
    *,
    seq: int,
    ref: Any = None,
    ts: float | None = None,
) -> bytes:
    """编码一条信封为 UTF-8 NDJSON 行（含换行）。"""
    envelope = {
        "v": PROTOCOL_VERSION,
        "seq": seq,
        "ts": time.time() if ts is None else ts,
        "type": msg_type,
        "ref": ref,
        "payload": {} if payload is None else payload,
    }
    text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def check_hello(payload: dict) -> str | None:
    """校验 hello 版本字段；通过返回 None，否则返回中文错误说明。"""
    protocol = payload.get("protocol_version")
    game = payload.get("game_version")
    problems = []
    if protocol != PROTOCOL_VERSION:
        problems.append("protocol_version=%r（需要 %r）" % (protocol, PROTOCOL_VERSION))
    if game != GAME_VERSION:
        problems.append("game_version=%r（需要 %r）" % (game, GAME_VERSION))
    if problems:
        return "版本不匹配：" + "，".join(problems)
    return None


def welcome_payload(session_id: str | None) -> dict:
    """构造 welcome 载荷（session_id 回显 hello）。"""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "session_id": session_id,
        "config": dict(DEFAULT_WELCOME_CONFIG),
    }


def error_payload(code: str, message: str) -> dict:
    """构造 error 载荷；mod 仅记录日志，不据此重连（v1）。"""
    return {"code": code, "message": message}


def _as_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError("字段 %s 必须是整数，得到 %r" % (name, value))
    return value


def _as_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError("字段 %s 必须是数字，得到 %r" % (name, value))
    return float(value)
