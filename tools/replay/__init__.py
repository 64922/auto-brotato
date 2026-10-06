"""离线回放工具（票据 03）：读取 agent 录制文件，按时间轴重放并统计。

模块：
- ``recording``：录制解析/校验/统计（流式，纯文件）；
- ``engine``：决策引擎接口与占位实现；
- ``replay``：重放与 CLI（``python -m tools.replay``）。
"""
from .recording import (
    Record,
    Recording,
    RecordingError,
    SnapshotStats,
    Summary,
    format_summary,
    iter_records,
    open_recording,
    summarize,
)
from .replay import replay

__all__ = [
    "Record",
    "Recording",
    "RecordingError",
    "SnapshotStats",
    "Summary",
    "format_summary",
    "iter_records",
    "open_recording",
    "summarize",
    "replay",
]
