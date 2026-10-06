# 票据 05：mod 菜单观测（难度页 + 终局）与协议 v2

- Status: ready-for-agent
- Blocked by: 04
- 关联：[protocol.md §7.1](../../../docs/protocol.md)、架构 §6.2、ADR-0009

## 背景

agent 需要检测"用户停在难度选择页"并读取 F1 信息；终局需要 result 供战报。

## 目标

1. `menu_observation.gd`：
   - 定位难度页场景；产出 `phase=difficulty_select` payload：`character.id`、`weapons`、`difficulty{selected,max_selectable,unlocked}`、`modes`、`can_start`（字段以票据 04 结论为准）；
   - 终局检测：`phase=run_end` payload：`result`、`wave`、统计摘要；
   - 推送策略与 `shop` 一致：内容变化推送 + 1s 心跳；离场不推送。
2. `mod_main.gd` 集成：新消息类型 `menu` 的泵送与断开清理。
3. `PROTOCOL_VERSION = 2`；`hello` 上报 v2（welcome 校验见票据 07 的 agent 侧）。
4. 实机验证：进/出难度页的字段正确；胜利/死亡各触发一次正确 result。

## 验收

- [ ] 难度页出现后 ≥1s 内 agent 收到完整 payload；离开后停止推送
- [ ] 英雄/武器/难度/开关字段与画面一致（人工比对）
- [ ] 终局 result 与实际一致（胜、败各一局）
- [ ] 旧 agent（v1）与新 mod 的拒绝路径符合 ADR-0009（不匹配即拒绝）

## 备注

- 只读；不做任何动作（动作在票据 06）。
- 引擎兼容严格遵循 ADR-0006。
