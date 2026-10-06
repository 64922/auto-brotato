"""回放驱动的决策引擎接口（本票先留空实现，票据 09–12 接入真实分层引擎）。"""
from __future__ import annotations

from typing import Protocol


class DecisionEngine(Protocol):
    """回放中每条 in 消息（协议信封 dict）的消费接口。"""

    def on_message(self, envelope: dict) -> None: ...


class NullDecisionEngine:
    """占位引擎：只消费消息、不产生动作，供回放链路与回归测试使用。"""

    def on_message(self, envelope: dict) -> None:
        return None
