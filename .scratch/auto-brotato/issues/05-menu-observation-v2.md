# 票据 05：mod 菜单观测（难度页 + 终局）与协议 v2

- Status: ready-for-human
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

- [x] 难度页出现后 ≥1s 内 agent 收到完整 payload；离开后停止推送
- [x] 英雄/武器/难度/开关字段与画面一致（人工比对）
- [x] 终局 result 与实际一致（胜、败各一局）
- [x] 旧 agent（v1）与新 mod 的拒绝路径符合 ADR-0009（不匹配即拒绝）

## 备注

- 只读；不做任何动作（动作在票据 06）。
- 引擎兼容严格遵循 ADR-0006。

## Comments

- 2026-10-06：票据 05 实现完成；分支 `ticket/05-menu-observation-v2`（worktree `C:\Users\33755\Desktop\auto-brotato-wt-05`）保留供独立验收，未合并、未推送。
- 实现：新增 `mod/src/menu_observation.gd`（难度页/终局只读观测；双路径定位场景根 + 屏幕可见性判定，排除滞留页面）；`mod/mod_main.gd` 接入 `_pump_menu`（10Hz 采样、内容变化 + 1s 心跳，策略与 shop 一致）与断线 `_menu.invalidate()`；`mod/src/ipc_client.gd` `PROTOCOL_VERSION := 2`；`tools/smoke_mod.py` 同步 v2；`docs/protocol.md` §7.1 字段表按实机更新。
- 接口变化（相对票据 04 初稿）：`modes` 权威来源改为 `RunData.is_endless_run` / `is_ban_mode_active` / `is_coop_run`（开关 UI 在选人页 `RunOptionsPanel`，离开选人页即释放，难度页读不到；难度页根变量 `add_random_element` / `enable_coop_panels` 语义未证实，仅记录）；zone 选择暂不输出；`run_end` 增补 `title`（`%Title.text`），`stats` 定型为 StatsContainer 各 `stat_container.gd` 格子 `key → Value` 文本（实测 40 项）。
- 实机验证（本机）：
  - 难度页 payload：进入后 ~0.25s 到达（< 1s），其后心跳精确 1s；英雄 `character_ranger`、武器 `weapon_pistol_1`、`difficulty.options` 7 项（D0/D1 已解锁、其余锁定；`is_locked` / `unlocked_by_default` / `value` 与画面一致）、`selected` / `displayed` / `can_start=true`；`endless` 开（true）与关（false）两次 payload 均与画面一致，其余模式 false。
  - 离场停推：离开难度页开局后 MENU 停发（menu 计数冻结）而 snapshot 持续 60Hz；终局按 `NewRunButton` 离开后同样停发（末条 MENU 17:44:58.136 → 下一次难度页 17:45:02.188 之间无 MENU，snapshot 连续）。
  - 终局：战败为 `result=defeat`、`wave=10`、`title=战败 - 碰撞区域`、stats 完整；胜利为 `result=victory`、`wave=20`、`title=胜利 - 碰撞区域`、stats 40 项与画面逐项一致（如 `CURRENT_LEVEL=27`、`STAT_MAX_HP=40`、`STAT_RANGE=245`）；胜利局由人工游玩取得。
  - 旧 agent v1 拒绝路径（ADR-0009）：v1 客户端收到 hello(protocol=2) 后回 `error`（`版本不匹配：protocol_version=2（需要 1）`）并断开；mod 侧会话被拒后连接计数保持 1（无重连风暴）。
- 剩余限制/观察：① `level_up` 与全部 `menu_*` 动作属票据 06，本票未实现；② agent 侧 v2 hello 校验/welcome 属票据 07；③ 胜利样本依赖人工游玩（无战斗 AI），仅此一局；④ `zone` 语义未证实，未输出；⑤ `RunData.difficulty_unlocked` 实测恒为 -1（候选来源，不依赖）；⑥ mod 日志 `连接断开（rejected），4000ms 后重试` 措辞与实际（不重连）不符，属既有代码 `ipc_client.gd`，本票未改，建议票据 07 顺带修正；⑦ 票据 04 spike 探针仍在，待 05/06 均落地后删除。
- 证据（本机，未入库）：接收端日志 `%TEMP%\opencode\t05_receiver.log`（首轮：难度页字段/离场停推/败局/v1 拒绝）与 `t05_fix_receiver.log`（修复后：`endless` 开关、胜利局）；临时 v2 接收端 `t05_receiver.py`、spike 命令助手 `t05_spike_cmd.py`；胜利局 stats 由用户实机截图比对。
- 修复记录：初版实现从难度页找 `RunOptionsPanel` 读模式开关，实机发现该面板属选人页且已释放（票据 04「开关位于选人页」结论正确），改为从 `RunData` 读取后实机复验通过。
- code-review 双轴复核（`4ff1c67...HEAD`）已修：① `title` 改 `%Title` 主路径 + 固定路径回退（ADR-0006 双路径；`%Title` 已在最终构建实测生效）；② `wave` 在 `RunData` 不可读时给 `null`（与 `result` 一致，避免被读成第 0 波）；③ 删除无调用方的 `is_open()`；④ `docs/protocol.md` hello/welcome 示例改 v2、`difficulty.displayed` 与实现对齐（玩家 0 `my_id` 数组）、§7.3 版本校验职责按票据 05/07 分工澄清；⑤ `docs/architecture.md` 难度页「滑杆式」与 §6.2 payload 字段按票据 04/05 实机修订。未采纳（判断项，理由）：`_pump_shop`/`_pump_menu` 形状重复（有意保留并注释说明，抽共享泵需引入状态对象、收益不足）；`modes` 中 `add_random_element` / `enable_coop_panels`（票据 04 记录项，协议标注「仅记录」）；状态标签沿用本仓既有约定（01–04 完成后同为 `ready-for-human`）。
- 最终构建重验（复核修复后重新部署，自动导航 smoke，部署 zip 与 HEAD 一致）：HELLO `protocol=2`；难度页 payload 完整（ranger / pistol_1 / 7 难度项（D0/D1 解锁）/ selected=D0 / modes 全 false / can_start=true）；终局 `result=defeat`、`wave=4`、`title=战败 - 碰撞区域`（`%Title` 主路径实测）、stats 40 项；心跳约 1.08s。证据：`%TEMP%\opencode\t05_final_receiver.log`。
