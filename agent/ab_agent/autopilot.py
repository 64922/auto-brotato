"""占位自动驾驶（票据 07）：只保证端到端流程闭环，正式策略见票据 09/10。

- 战斗：随机小范围游走（0.6–1.2s 换向，贴近场地边缘时朝场内修正），
  以 ≤0.12s 间隔续发 move（mod 侧 TTL 250ms 兜底安全停住）；
- 不做避弹、目标选择与评分，不读知识库。

本模块不持有 IO：`next_move` 由 RunSession 在 tick 中调用并按返回值下发动作。
"""
from __future__ import annotations

import math
import random
from typing import Optional, Sequence

#: move 续发间隔（秒）；小于 TTL 的一半，保证 8Hz+ 链路不断流
MOVE_INTERVAL_S = 0.12

#: 换向间隔范围（秒）
DIRECTION_MIN_S = 0.6
DIRECTION_MAX_S = 1.2

#: 贴边修正边距（px）
EDGE_MARGIN = 80.0


class PlaceholderAutopilot:
    """随机小范围游走的占位控制器。"""

    def __init__(
        self,
        *,
        rng: Optional[random.Random] = None,
        move_interval_s: float = MOVE_INTERVAL_S,
    ) -> None:
        self._rng = rng or random.Random()
        self._move_interval_s = move_interval_s
        self.reset()

    def reset(self) -> None:
        self._last_move_at: Optional[float] = None
        self._direction = (0.0, 0.0)
        self._direction_until = 0.0

    def next_move(self, snapshot: dict, now: float) -> Optional[list[float]]:
        """到续发间隔时返回 move 向量，否则返回 None。"""
        if self._last_move_at is not None and now - self._last_move_at < self._move_interval_s:
            return None
        self._last_move_at = now
        if now >= self._direction_until or self._direction == (0.0, 0.0):
            angle = self._rng.uniform(0.0, 2.0 * math.pi)
            magnitude = self._rng.uniform(0.35, 0.7)
            self._direction = (math.cos(angle) * magnitude, math.sin(angle) * magnitude)
            self._direction_until = now + self._rng.uniform(DIRECTION_MIN_S, DIRECTION_MAX_S)
        dx, dy = self._direction
        player = snapshot.get("player") or {}
        pos = player.get("pos") if isinstance(player, dict) else None
        arena = snapshot.get("arena")
        if isinstance(arena, dict) and isinstance(pos, Sequence) and len(pos) == 2:
            dx, dy = _avoid_edges(dx, dy, float(pos[0]), float(pos[1]), arena)
        return [round(dx, 3), round(dy, 3)]


def _avoid_edges(dx: float, dy: float, px: float, py: float, arena: dict) -> tuple[float, float]:
    low = arena.get("min")
    high = arena.get("max")
    if not (
        isinstance(low, Sequence)
        and isinstance(high, Sequence)
        and len(low) >= 2
        and len(high) >= 2
    ):
        return dx, dy
    try:
        min_x, min_y, max_x, max_y = float(low[0]), float(low[1]), float(high[0]), float(high[1])
    except (TypeError, ValueError):
        return dx, dy
    if px < min_x + EDGE_MARGIN:
        dx = abs(dx)
    elif px > max_x - EDGE_MARGIN:
        dx = -abs(dx)
    if py < min_y + EDGE_MARGIN:
        dy = abs(dy)
    elif py > max_y - EDGE_MARGIN:
        dy = -abs(dy)
    return dx, dy
