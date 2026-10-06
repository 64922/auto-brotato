# 票据 07：agent 状态机与难度交互（终端 CLI 闭环）

- Status: ready-for-agent
- Blocked by: 02, 05
- 关联：架构 §4、ADR-0004、[protocol.md §2.3](../../../docs/protocol.md)

## 背景

核心用户流程：检测难度页 → 终端打印信息 → 用户输入难度 → 设难度 → 开局 → 对局 → 结束报告。

## 目标

1. 状态机：`IDLE → MENU_READY → AWAIT_INPUT → STARTING → RUNNING(COMBAT/SHOP/LEVEL_UP) → ENDED → IDLE`（架构 §4）。
2. 难度交互：
   - 收到 `menu=difficulty_select` 后打印：英雄（ID + 中文名映射）、初始武器、可选难度（`difficulty.unlocked` 范围）、模式开关、当前滑杆值；
   - 接受 `D0`–`Dn`（大小写不敏感，n=解锁上限）；非法/超范围/空输入重问；输入 `q` 取消退出；
   - **开始前二次确认**模式开关（特别 endless/ban 为开时，R10）。
3. 执行：`menu_set_difficulty → 读回校验 → menu_start_run → 等待 wave.index==1 && phase=="combat"`；超时/失败给出提示并回到 `AWAIT_INPUT` 或报错停止（按错误类型）。
4. v2 版本校验（agent 侧）：`hello.protocol_version==2` 且 `game_version==1.1.15.4`，否则 `error` 拒绝（ADR-0005/0009）。
5. `stop/resume`：stop 断开让出；断线重连进入 `OBSERVE_ONLY`，`resume` 恢复接管。
6. 终局：识别 `menu=run_end`（或死亡兜底）→ 打印战报（结果、波次、时长、金币、构建、回放路径）→ 回 `IDLE`。

## 验收

- [ ] 全程实机演示：从难度页到开局，无人工鼠标/键盘操作（除终端输入）
- [ ] 非法输入（如 `D9`、`abc`）被正确拒绝并重问
- [ ] 模式开关开启时会显示并需确认
- [ ] 断线 → 人物停住；重连后不发 move 直到 `resume`
- [ ] 终局打印战报并回到待命

## 备注

- 战斗阶段先接一个"占位走位"（如随机小范围移动或原地），正式策略在票据 09/10；本票据只验证流程。
- 商店/升级阶段先打日志并执行固定简单动作（如直接离开/选第一项），闭环即可。
