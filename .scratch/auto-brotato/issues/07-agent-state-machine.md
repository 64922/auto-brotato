# 票据 07：agent 状态机与难度交互（终端 CLI 闭环）

- Status: ready-for-human
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

- [x] 全程实机演示：从难度页到开局，无人工鼠标/键盘操作（除终端输入）
- [x] 非法输入（如 `D9`、`abc`）被正确拒绝并重问
- [x] 模式开关开启时会显示并需确认
- [x] 断线 → 人物停住；重连后不发 move 直到 `resume`
- [x] 终局打印战报并回到待命

## 备注

- 战斗阶段先接一个"占位走位"（如随机小范围移动或原地），正式策略在票据 09/10；本票据只验证流程。
- 商店/升级阶段先打日志并执行固定简单动作（如直接离开/选第一项），闭环即可。

## Comments

- 2026-10-06：票据 07 实现完成；分支 `ticket/07-agent-state-machine`（worktree `C:\Users\33755\Desktop\auto-brotato-wt-07`）保留供独立验收，未合并、未推送。
- 实现：新增 `agent/ab_agent/session.py`（`RunSession` 状态机：难度页识别/输入校验/模式确认/开局序列/终局与死亡兜底/stop-resume）、`run_control.py`（`RunController`：占位走位 + 商店离开 + 升级选卡 + 战报记账）、`menu_view.py`（难度页/终局/商店/升级解析与终端文案）、`autopilot.py`（`PlaceholderAutopilot` 随机游走 + 贴边修正）、`hero_names.py`（英雄中文名临时表）；`state.py` 新增 menu 观测（`fresh_menu/fresh_shop/fresh_snapshot`、`MENU_FRESH_S=SHOP_FRESH_S=2.5`、`SNAPSHOT_FRESH_S=1.5`）；`protocol.py` 升 v2 并新增 `ack_ok/ack_error`；`ipc_server.py` 分发 `menu`；`cli.py` 接入会话 tick（50ms）与带 flush 输出；`docs/architecture.md` §3/§4/§11 同步。
- 接口/语义变化（相对票据 02/06）：
  - `PROTOCOL_VERSION=2`；agent 侧 `check_hello` 校验 `protocol_version==2 && game_version==1.1.15.4`，不匹配回 `error` 拒绝（ADR-0005/0009）。
  - 接管语义：本进程**首次连接即接管**（PRD 主流程），**断线重连默认 OBSERVE_ONLY**、需 `resume`；取代票据 02 的「每次握手都 OBSERVE_ONLY」。
  - CLI 输入：难度页 `D0–Dn`（大小写不敏感）/回车重打印/`q` 取消；`q` 在非难度选择阶段仍为退出（恢复票据 06 行为），确认页 `y/n/q`。
  - 失败处理：ack 失败/读回超时/开局 10s 超时 → 提示并回 `AWAIT_INPUT`（页面仍在且已接管）或 `IDLE`（页面离开/断线）。
  - 超时参数：ack 2.0s、读回 1.5s、开局等待 10.0s、死亡兜底 3.0s、固定动作重试冷却 1.5s、menu/shop 新鲜 2.5s、快照 1.5s。
- 验证结果：
  - 自动化：pytest 与 unittest 均 103 passed（含 FakeServer 单元、FakeMod 真实 TCP 端到端）；CLI 级假 mod 端到端 19 项检查全过（`%TEMP%\opencode\t07_cli_e2e.py`）。
  - 实机（Brotato 1.1.15.4 + 部署的 mod 0.2.0，三局）：
    - 难度页：~0.25s 收到 payload；打印英雄 `character_ranger（游侠）`、武器、可选 `D0–D1`、模式开关、当前选择，与画面一致。
    - 非法输入：`abc` → 「无法识别的输入」、`D9` → 「超出当前已解锁范围（D0–D1）」并重问（实机验证）。
    - 开局：`D0` → 读回校验通过 → `menu_start_run` → 第 1 波 combat（实机验证）；占位走位持续下发，人物正常移动。
    - 对局内：商店打印摘要后固定离开（3 次）；升级页选第一张卡（多次，见限制②）。
    - `stop`：主动断开，人物停住；2s 后 mod 自动重连并进入 OBSERVE_ONLY（未发任何 move）；`resume` 后恢复接管（实机验证）。
    - 终局：第 4 波阵亡 → 打印战报（结果/波次/时长/金币/构建/回放路径/死亡兜底备注）→ 「已回到待命（IDLE）」（实机验证；该局 mod 未发 `run_end`，死亡兜底路径生效；`run_end` 主路径由自动化端到端覆盖）。
    - 模式确认：难度页打开「无尽模式」→ 显示「模式开关已开启：无尽模式。确认按此模式开始 D0 对局？」→ `y` → 设难度/开局（实机验证；但见限制④）。
  - 证据（未入库）：回放 `recordings/20261006-192913-unknown-unknown.ndjson`、`recordings/20261006-193631-unknown-unknown.ndjson`、`recordings/20261006-194129-unknown-unknown.ndjson`；实机日志 `%TEMP%\opencode\t07_live_output.txt`（完整一局含 stop/resume/战报）、`t07_live_mode_output.txt`（无尽确认）。
- code-review 双轴复核（`main...HEAD` 未提交变更）已修：① `session.py` 669 行超 500 行约束 → 拆出 `run_control.py`（RUNNING 阶段动作/记账与阶段状态机分离）；② CLI `q` 恢复退出语义；③ 第二局战报串上一局金币/构建（`RunController.begin()` 清记账）；④ 取消难度后离开再重进同一页面不再提示（离场清理被相位条件挡住）；⑤ `MENU_READY`/`ENDED` 死状态落地（OBSERVE_ONLY 等待位；战报当轮后下一 tick 回 IDLE）；⑥ 去重（`ack_ok/ack_error` 入 `protocol.py`；`result_text`、`MODE_LABELS`、`difficulty_my_id/difficulty_value` 收敛）；⑦ `format_duration` 去掉 Middle Man 包装、删除无调用 `hero_name()`；⑧ 架构文档同步（§3 模块图、§4 状态图 + CONFIRM、§4.3 商店 2.5s、§11 仓库树）。未采纳（判断项）：`RunController` 构造参数包（依赖注入，不视为 Data Clumps）；难度值以 int + `difficulty_` 字符串存在（协议 payload 原样，视图层已有解析辅助）。
- 剩余限制/已知问题：
  1. `hero_names.py` 为人工整理的 50 个英雄中文名临时表（未收录回退原始 ID）；票据 11 知识库落地后替换。
  2. 升级选卡实机出现 `not_in_level_up`（选卡动作到达时游戏已关闭菜单），按 1.5s 冷却用新菜单重试；占位逻辑可接受，正式选卡在票据 12。
  3. 战报「道具」列表含 `character_ranger`（mod `inventory.items` 含角色条目），展示层待过滤（票据 11/12 或后续小修）。
  4. **无尽模式开局被游戏原生警告弹窗挡住**：`menu_start_run` 无法跨过该弹窗，agent 10s 超时后优雅回到 `AWAIT_INPUT`；agent 侧「显示 + 需确认」已满足验收，mod 侧是否补确认动作留待后续票据（涉及 05/06 范围，本票未扩围）。
  5. 死亡时 mod 未发 `run_end`（3s 死亡兜底生效）；若后续确认 `run_end` 实际到达时机，可再调兜底时长。
  6. 录制 header 的 hero/difficulty 仍为 `unknown-unknown`（票据 03 遗留字段，本票未改）。
  7. 实机演示中英雄/武器选择与难度页导航为人工操作（合成输入在本引擎不可用的既有结论），符合「除终端输入」前提。
