#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools 共用的最小 NDJSON IPC 链路（仅标准库）。

mod 为客户端、工具为一次性服务端：mod 主动连出后，双方以 NDJSON 信封
（协议 v2，``agent/ab_agent/protocol.py`` 为准）双向收发。

被 ``tools/smoke_mod.py``（票据 01）与 ``tools/export_knowledge.py``（票据 11）
共用；工具可能以脚本（``python tools/xxx.py``）或包（pytest）方式加载，
调用方用 try/except 兼容两种导入路径。
"""
from __future__ import annotations

import json
import select
import time

#: 与 agent/mod 协商的协议版本（协议 v2）
PROTOCOL_VERSION = 2


class Link:
    """mod 主动连出后的最小双向 NDJSON 链路。"""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.seq = 0
        self.pending = []

    def send(self, msg_type, payload, ref=None):
        envelope = {
            "v": PROTOCOL_VERSION,
            "seq": self.seq,
            "ts": time.time(),
            "type": msg_type,
            "ref": ref,
            "payload": payload,
        }
        self.seq += 1
        line = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n"
        self.sock.sendall(line.encode("utf-8"))

    def pump(self, timeout=0.05):
        readable, _, _ = select.select([self.sock], [], [], timeout)
        if not readable:
            return
        data = self.sock.recv(65536)
        if not data:
            raise ConnectionError("mod 关闭了连接")
        self.buf += data
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(message, dict):
                self.pending.append(message)

    def drain(self):
        out, self.pending = self.pending, []
        return out


def wait_for_hello(link, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        link.pump(0.1)
        for message in link.drain():
            if message.get("type") == "hello":
                return message.get("payload", {})
    return None
