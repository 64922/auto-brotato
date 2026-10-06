# 票据 06：mod 菜单动作 + 升级选卡观测与动作

- Status: ready-for-agent
- Blocked by: 04
- 关联：[protocol.md §7.1.2/§7.2](../../../docs/protocol.md)、ADR-0007

## 背景

需要从难度页设置难度并开始对局；波间升级选卡必须自动选择才能打完整局。

## 目标

1. `menu_actions.gd`（语义与 `shop_actions.gd` 对齐：幂等账本、串行、超时、ack）：
   - `menu_set_difficulty {value}`：按差值步进加减按钮（或票据 04 结论的等价路径）；越界/不在难度页 → `bad_difficulty`/`not_in_difficulty_select`；设置后读回校验；
   - `menu_start_run {}`：经开始按钮信号；`can_start=false` → `cannot_start`；
   - `menu_pick_upgrade {index}`：升级卡选择；越界 → `bad_index`；不在升级页 → `not_in_level_up`；
   - 其余 `menu_*` 一律 `unsupported_kind`。
2. `menu_observation.gd` 扩展：`phase=level_up` payload（`wave` + `options[{slot,kind,id,tier}]`，结构以票据 04 为准）。
3. 实机验证三项动作各成功一次；重复 ref 幂等；断连后在途动作 `link_lost`。

## 验收

- [ ] 难度滑杆从任意初值移动到目标值（读回校验）并成功开局
- [ ] 升级选卡动作实际选中目标卡（差分验证）
- [ ] 幂等/串行/超时用例通过（可与现有 shop 动作相同用例复用）
- [ ] `menu_pause` 未实现（或实现为可选开关），协议错误码清晰

## 备注

- `menu_pause` 为可选增强（架构 §13 未决）；若不实现，动作表标注"未支持"。
- 所有路径走游戏正常 UI 信号（ADR-0007）。
