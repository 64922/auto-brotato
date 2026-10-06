# 票据 03：回放录制与离线回放工具骨架

- Status: ready-for-agent
- Blocked by: 02
- 关联：ADR-0008、[strategy.md §8](../../../docs/strategy.md)

## 背景

真实对局 20–30 分钟，决策迭代必须靠回放。agent 在运行中同步落盘 NDJSON。

## 目标

1. `agent/ab_agent/recorder.py`：录制文件（默认 `recordings/<时间戳>-<英雄>-<难度>.ndjson`）：
   - header 行：`{kind:"header", agent_version, mod_version, protocol_version, game_version, hero_id, weapons, difficulty, window:{...}}`；
   - 数据行：`{kind:"in"/"out", ts, envelope}`（mod→agent 全部消息、agent→mod 全部动作）；
   - `{kind:"decision", ts, layer, action, reason}`：关键决策理由（P2/P3 接入；先留接口）。
2. `tools/replay/`：读取录制文件，按时间轴重放 `in` 消息喂给决策引擎（接口先留空实现），支持 `--speed`、`--seek`、`--summary`（节点统计）。
3. 录制开关与体积控制（默认开启、可选压缩归档）；`recordings/` 不入库。

## 验收

- [ ] 一次实机运行产出完整回放（可独立解析，含 header 与环境字段）
- [ ] `--summary` 输出消息计数、时长、波次范围、快照频率统计
- [ ] 回放重放不依赖游戏（纯文件）

## 备注

- 录制应尽量原位不阻塞 IPC 读取（异步写或队列）；性能开销以不影响 60Hz 链路为准。
