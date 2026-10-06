# 票据 16：菜单动作偶发不生效（difficulty/start 需重试）

- Status: ready-for-agent
- Blocked by: 无
- 关联：票据 06（菜单动作）、07（会话状态机）、09（实机验收发现）

## 背景

票据 09 实机验收期间（2026-10-06 20:42–21:21，多局 D0 开局）观察到 mod 菜单动作偶发不生效，
agent 侧超时后重发即可成功（本次由实机驱动脚本自动重试兜底）：

1. `menu_set_difficulty` 焦点移动超时：难度页初始选中 D1、目标 D0；
   `difficulty_selection.gd` 路径为 `element.grab_focus()` + 等待 `selected_difficulty_id()` 变化，
   实测一次在 `DIFFICULTY_TIMEOUT_MS` 内未观测到变化（`error=difficulty_timeout`，20:42:12）。
   同一进程首局 D1→D0 曾成功（20:33:30），故为偶发而非必现。
2. `menu_start_run` 首次按压未开局：`element.emit_signal("pressed")` 后 10s 未观测到第 1 波，
   难度页仍在（20:43:55）；重发 `menu_set_difficulty`（空操作）+ `menu_start_run` 后成功（20:46:41）。

## 目标

1. 复现并定位两次偶发：区分「UI 尚未就绪/焦点事件未落地」与「信号连接时序」，
   必要时在动作前等待/重试（mod 内部），而不是依赖 agent 侧超时重发。
2. 幂等/超时语义保持票据 06 口径；不可用无边界重试掩盖问题。
3. 给出可观测证据（modloader.log 或动作回执携带重试计数）。

## 验收

- [ ] 连续 ≥10 次开局（含 D1→D0 焦点移动）无人工/驱动重试，或每次重试均有明确日志归因
- [ ] 单元/实机用例覆盖：动作重试路径可复现（可注入延迟模拟 UI 未就绪）

## 备注

- 证据留存：`%TEMP%\opencode\t09-live-*.log`（20:33/20:42/20:43/20:46 时间线）。
- 与票据 15 独立；两者共同影响「除难度输入外无人工干预」的验收口径。
