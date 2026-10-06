# ADR-0007：动作注入一律走游戏正常输入与 UI 信号

- 状态：已接受（2026-10-06，据既有 mod 0.2.0 代码注释重建；原 ADR 文本已丢失）
- 关联：[architecture.md](../architecture.md) §3.1、[protocol.md](../protocol.md) §5

## 背景

让机器人控制游戏有两种路径：直接修改游戏状态（设坐标/改金币/塞背包），或经游戏自身的输入与 UI 流程触发正常逻辑。前者实现快但破坏游戏规则、易脏存档且行为不可解释。

## 决策

所有动作只经两条正常通道注入：

1. **移动**：Godot `Input` 单例（`Input.action_press/release`，数字 8 向 + PWM 模拟比例；本引擎合成手柄轴事件不可用，实测结论）；
2. **UI 操作**：发射与 UI 按钮等价的信号，由游戏自身逻辑完成状态变更。例：
   - 购买：`BuyButton.pressed`
   - 卖出：`ItemPopup.item_discard_button_pressed(weapon)`
   - 刷新：`RerollButton.pressed`
   - 锁定：`LockButton.toggled(locked)`
   - 离开：`GoButton.pressed`
   - 菜单（P1）：开始按钮信号；难度滑杆以按钮步进等价路径设置。

绝不直接写玩家坐标、速度、金币、背包或场景对象。

## 理由

- 行为等价于真实玩家操作：存档安全、结果可解释、与游戏规则天然一致；
- 复用游戏的校验/动画/信号回执，动作结果可验证（如购买等 `shop_item_bought` 信号）；
- 是本系统"合规自动化"定位的核心不变量。

## 后果

- 动作执行依赖 UI 节点存在与命名（易碎点，走 ADR-0006 约定）；
- 部分操作需要差分验证（卖出/刷新在 mod 侧对比前后状态）；
- 幂等、串行、超时机制由 mod 侧实现（protocol.md §5）。
