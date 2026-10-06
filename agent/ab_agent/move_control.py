"""走位控制器接口（对局编排与回放评估共用）。"""
from __future__ import annotations

from typing import Optional, Protocol


class MoveController(Protocol):
    """走位控制器接口：``ReflexController`` 与 ``PlaceholderAutopilot`` 均满足。"""

    def reset(self) -> None: ...

    def next_move(self, snapshot: dict, now: float) -> Optional[list[float]]: ...
