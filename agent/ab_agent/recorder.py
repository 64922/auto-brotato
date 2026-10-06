"""回放录制：把一次会话的全部 IPC 消息落盘为 NDJSON（ADR-0008、票据 03）。

文件格式（每行一个 JSON 对象）：
- ``{"kind": "header", ts, agent_version, mod_version, protocol_version, game_version,
  session_id, hero_id, weapons, difficulty, window}`` —— 会话开始时写入一次；
  hero/weapons/difficulty/window 在 P0（协议 v1 无菜单观测）为 null，票据 05/06
  接入菜单观测后由调用方传入；
- ``{"kind": "in"|"out", ts, envelope}`` —— mod→agent 全部消息（in）与
  agent→mod 全部动作（out）；envelope 为协议信封原文；
- ``{"kind": "decision", ts, layer, action, reason}`` —— 关键决策理由，
  P2/P3（票据 09–12）接入，本模块先留接口。

稳态写入经后台线程 + 有界队列完成：``record_*`` 只入队（微秒级），JSON 序列化与
磁盘 IO 都在写入线程，不阻塞 60Hz IPC 链路；队列满（磁盘长时间卡顿）或写入失败时
丢弃后续记录并计数告警，内存有上限。``end_session()`` 排空队列并关闭文件（会话已
结束时同步完成，不影响运行中的链路）。``compress=True`` 时直接写 ``.ndjson.gz``。
"""
from __future__ import annotations

import gzip
import json
import logging
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

LOGGER_NAME = "ab.recorder"

#: end_session 等待写入线程排空的超时（避免磁盘异常时永久阻塞事件循环）
DRAIN_JOIN_TIMEOUT_S = 10.0

#: 待写队列上限（约 17s 的 60Hz 快照）：写入长期落后时丢弃并告警，内存有界
RECORD_QUEUE_MAX = 1024

_STOP = object()


def recording_filename(
    ts: float,
    hero_id: Any = None,
    difficulty: Any = None,
    *,
    compress: bool = False,
) -> str:
    """构造录制文件名：``<时间戳>-<英雄>-<难度>.ndjson[.gz]``。

    P0 尚无英雄/难度信息时用 ``unknown`` 占位；difficulty 为整数时写作 ``D<n>``。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
    suffix = ".ndjson.gz" if compress else ".ndjson"
    return "%s-%s-%s%s" % (stamp, _slug(hero_id), _slug(difficulty), suffix)


def _slug(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, int) and not isinstance(value, bool):
        text = "D%d" % value
    else:
        text = str(value)
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "-", text).strip("-.")
    return safe or "unknown"


class Recorder:
    """一次会话对应一个录制文件；重连（新会话）自动开新文件。"""

    def __init__(
        self,
        directory: str | Path,
        *,
        compress: bool = False,
        logger: logging.Logger | None = None,
        clock=time.time,
        queue_max: int = RECORD_QUEUE_MAX,
    ) -> None:
        self.directory = Path(directory)
        self.compress = compress
        self.log = logger or logging.getLogger(LOGGER_NAME)
        self._clock = clock
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=queue_max)
        self._thread: Optional[threading.Thread] = None
        self._file = None
        self._path: Optional[Path] = None
        self._owner: Any = None
        self._failed = False
        self._dropped = 0

    # ---- 会话生命周期 ----

    @property
    def recording_path(self) -> Optional[Path]:
        """当前会话的录制文件路径；无活动会话返回 None。"""
        return self._path

    @property
    def dropped(self) -> int:
        """当前会话因队列满/写入失败丢弃的记录数（可观测性）。"""
        return self._dropped

    def start_session(
        self,
        *,
        hello: dict,
        agent_version: str,
        owner: Any = None,
        hero_id: Any = None,
        weapons: Any = None,
        difficulty: Any = None,
        window: Any = None,
    ) -> Optional[Path]:
        """开始录制（先结束旧会话）；返回录制文件路径。

        打不开目录/文件（磁盘、权限等）时记录错误并让本会话不录制：
        录制失败不允许影响 IPC 链路。
        """
        self.end_session()
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            ts = self._clock()
            filename = recording_filename(ts, hero_id, difficulty, compress=self.compress)
            path = _unique_path(self.directory / filename)
            opener = gzip.open if self.compress else open
            self._file = opener(path, "wt", encoding="utf-8", newline="\n")
        except OSError as exc:
            self._file = None
            self._path = None
            self.log.error("无法开始录制（%s），本会话不录制：%s", self.directory, exc)
            return None
        self._path = path
        self._failed = False
        self._dropped = 0
        self._owner = owner
        header = {
            "kind": "header",
            "ts": ts,
            "agent_version": agent_version,
            "mod_version": hello.get("mod_version"),
            "protocol_version": hello.get("protocol_version"),
            "game_version": hello.get("game_version"),
            "session_id": hello.get("session_id"),
            "hero_id": hero_id,
            "weapons": weapons,
            "difficulty": difficulty,
            "window": window,
        }
        self._queue.put(header)
        self._thread = threading.Thread(target=self._drain, name="ab-recorder", daemon=True)
        self._thread.start()
        self.log.info("开始录制：%s", path)
        return path

    def end_session(self, *, owner: Any = None) -> None:
        """排空队列、关闭文件；``owner`` 不为 None 时仅结束该所有者发起的会话。

        可重复调用（幂等）；新连接替换旧连接时，旧连接的迟到清理不会误关新会话。
        """
        if owner is not None and owner is not self._owner:
            return
        thread = self._thread
        self._thread = None
        if thread is not None:
            self._stop_drainer()
            thread.join(timeout=DRAIN_JOIN_TIMEOUT_S)
            if thread.is_alive():
                self.log.warning("录制线程未在 %.0fs 内结束，可能丢失尾部记录", DRAIN_JOIN_TIMEOUT_S)
        if self._file is not None:
            try:
                self._file.close()
            except OSError as exc:
                self.log.error("关闭录制文件失败：%s", exc)
            self._file = None
        if self._path is not None:
            if self._dropped:
                self.log.warning(
                    "录制结束：%s（丢弃 %d 条：写入落后或失败）", self._path, self._dropped
                )
            else:
                self.log.info("录制结束：%s", self._path)
            self._path = None
        self._owner = None

    # ---- 记录接口（非阻塞，仅入队）----

    def record_in(self, envelope: dict) -> None:
        """记录一条 mod→agent 消息信封。"""
        self._enqueue({"kind": "in", "ts": self._clock(), "envelope": envelope})

    def record_out(self, envelope: dict) -> None:
        """记录一条 agent→mod 动作信封。"""
        self._enqueue({"kind": "out", "ts": self._clock(), "envelope": envelope})

    def record_decision(self, layer: str, action: Any, reason: str) -> None:
        """记录一条关键决策理由（P2/P3 接入）。

        layer/reason 统一转字符串，保证 tools/replay 的严格解析始终成立。
        """
        self._enqueue(
            {
                "kind": "decision",
                "ts": self._clock(),
                "layer": str(layer),
                "action": action,
                "reason": str(reason),
            }
        )

    def _stop_drainer(self) -> None:
        """投递停止哨兵；队列满时丢弃最旧记录以腾位（避免 end_session 死等）。"""
        while True:
            try:
                self._queue.put_nowait(_STOP)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self._dropped += 1
                except queue.Empty:
                    continue

    def _enqueue(self, record: dict) -> None:
        if self._thread is None:
            return
        if self._failed:
            self._dropped += 1
            return
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1:
                self.log.warning("录制队列已满（%d 条），写入落后于链路，开始丢弃记录", self._queue.maxsize)

    # ---- 写入线程 ----

    def _drain(self) -> None:
        while True:
            record = self._queue.get()
            if record is _STOP:
                return
            try:
                line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                self._file.write(line + "\n")
            except (OSError, ValueError, TypeError) as exc:
                self._dropped += 1
                if not self._failed:
                    self._failed = True
                    self.log.error("录制写入失败，后续记录丢弃：%s", exc)


def _unique_path(path: Path) -> Path:
    """同名文件已存在时追加 ``-<n>``（同一秒内多次开会话）。"""
    if not path.exists():
        return path
    suffix = ".ndjson.gz" if path.name.endswith(".ndjson.gz") else path.suffix
    base = path.name[: -len(suffix)]
    index = 2
    while True:
        candidate = path.with_name("%s-%d%s" % (base, index, suffix))
        if not candidate.exists():
            return candidate
        index += 1
