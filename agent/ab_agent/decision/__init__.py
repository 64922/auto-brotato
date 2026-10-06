"""决策引擎分层（architecture.md §7、strategy.md）。

反射层（reflex.py）是唯一直接生成 ``move`` 的模块（30–60Hz）；战术层（票据 10）与
经济层（票据 12）通过 :class:`ab_agent.decision.reflex.TacticalIntent` 等接口间接影响
移动。所有权重/阈值集中在 ``decision/config/*.json``，代码不写死（strategy.md §8）。
"""
