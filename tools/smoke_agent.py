#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冒烟用 agent 子进程封装（票据 08）：Popen + 逐行读取 + stdin 写入 + 优雅退出。

``read_line`` 超时返回 ``None``，进程输出结束返回哨兵 ``EOF``；
``close`` 先发 ``quit`` 等待自然退出，超时才强杀（保证录制刷盘）。
"""
from __future__ import annotations

import queue
import subprocess
import threading
from pathlib import Path
from typing import Optional, Sequence


EOF = object()


class SubprocessAgent:
    """以子进程运行 agent CLI，逐行读取输出并写入 stdin。"""

    def __init__(self, argv: Sequence[str], *, cwd: Optional[Path] = None, env: Optional[dict] = None) -> None:
        self.argv = list(argv)
        self._proc = subprocess.Popen(
            self.argv,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._lines: "queue.Queue[object]" = queue.Queue()
        self._thread = threading.Thread(target=self._pump, name="ab-smoke-reader", daemon=True)
        self._thread.start()
        self.eof = False

    def _pump(self) -> None:
        stdout = self._proc.stdout
        if stdout is not None:
            for line in stdout:
                self._lines.put(line.rstrip("\r\n"))
        self._lines.put(EOF)

    def read_line(self, timeout: float) -> object:
        """读取一行；超时返回 None，进程输出结束返回 EOF 哨兵。"""
        if self.eof:
            return EOF
        try:
            item = self._lines.get(timeout=max(0.01, timeout))
        except queue.Empty:
            return None
        if item is EOF:
            self.eof = True
        return item

    def send_line(self, text: str) -> None:
        stdin = self._proc.stdin
        if stdin is None:
            return
        try:
            stdin.write(text + "\n")
            stdin.flush()
        except (OSError, ValueError):
            pass

    def close(self, timeout: float = 20.0) -> Optional[int]:
        """发送 quit 并等待进程退出；超时则强杀。返回退出码（被杀返回 None）。"""
        self.send_line("quit")
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            try:
                return self._proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                return None
