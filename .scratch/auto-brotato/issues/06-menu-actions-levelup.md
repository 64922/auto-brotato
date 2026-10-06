# 票据 06：mod 菜单动作 + 升级选卡观测与动作

- Status: ready-for-human
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

- [x] 难度滑杆从任意初值移动到目标值（读回校验）并成功开局（语义按反编译修正为「焦点移动」，见 Comments）
- [x] 升级选卡动作实际选中目标卡（差分验证）
- [x] 幂等/串行/超时用例通过（可与现有 shop 动作相同用例复用）
- [x] `menu_pause` 未实现（或实现为可选开关），协议错误码清晰

## 备注

- `menu_pause` 为可选增强（架构 §13 未决）；若不实现，动作表标注"未支持"。
- 所有路径走游戏正常 UI 信号（ADR-0007）。

## Comments

- 2026-10-06：票据 06 实现完成；分支 `ticket/06-menu-actions-levelup`（worktree `C:\Users\33755\Desktop\auto-brotato-wt-06`）保留供独立验收，未合并、未推送。
- 实现：
  - `mod/src/menu_actions.gd`（新）：`menu_set_difficulty` / `menu_start_run` / `menu_pick_upgrade`；幂等账本（256）、串行队列（16，满回 `busy`）、超时回执（难度 700ms / 开局 1500ms / 选卡 1500ms）、断连在途 `link_lost`、排队丢弃；其余 `menu_*`（含 `menu_pause`）经 mod_main 统一 `unsupported_kind`。
  - `mod/src/level_up_observation.gd`（新）：`phase=level_up` payload（`player/wave/options[{slot,kind,id,tier,can_pick}]`）；双路径场景定位（实机路径 + `upgrades_ui.tscn` 全树扫描、排除 coop 变体、仅接受在屏实例）；向 `menu_actions` 暴露卡片/按钮/签名访问器。
  - `mod/src/menu_observation.gd`：新增只读访问器 `in_difficulty()` / `difficulty_element(value)` / `focused_element()` / `selected_difficulty_id()` / `can_start()` / `in_run_scene()`；`can_start` 改为焦点元素 `is_special != true`（反编译 `_on_element_pressed` 接受条件）。
  - `mod/mod_main.gd`：接入 `_menu_actions`（`_handle_action` + 每帧 `_flush_menu_actions` + 断线 `reset()`）与 `_level_up`（`_pump_menu` 中难度页优先，三页实机互斥；断线 `invalidate()`）。
  - 文档：`docs/protocol.md` §7.1.1/§7.1.2/§7.2/§8 与 `docs/architecture.md` §6.2/§13 按最终语义更新；票据 04 的三个 spike 探针文件及 mod_main 挂接段已删除。
- 接口/语义修正（相对票据 04 初稿，依据 Brotato.pck 反编译 + 实机复核）：
  - 难度页不是滑杆：`difficulty_selection.gd _on_element_pressed` 对任意**非 special** 元素一次 `pressed` 即置 `RunData.current_difficulty` 并立刻 `change_scene(main.tscn)` 开局（**无二次确认**；锁定/随机元素 `is_special=true`，pressed 被忽略）；焦点移动（`element_focused`）只更新 `_latest_focused_element` 与高亮面板、不触发开局。因此 `menu_set_difficulty` = 游戏同款 `grab_focus()` 移动焦点后按 `_latest_focused_element[0]` 读回（同值=空操作，避免副作用）；`menu_start_run` = 对焦点元素发 `pressed`。
  - 游戏**不存在 `run_started` 信号**（票据 04 记录有误，该字符串仅为 `ProgressData` 统计键名）；开局完成判定 = 难度页离场且 `/root/Main`（main.tscn）就位；`RunData.menu_selection_back` 会被目标菜单页 `_ready` 复位，跨帧判定不可靠（实机 timeout 轮误判后修正）。
- 实机验证（本机临时 receiver，日志未入库；均为最终代码）：
  - `mode=full` `t06_session3_full.log` 20/20：难度页 payload；set D1 ack+读回；同值空操作未开局；ref=103 幂等（重复回相同 ack 且不重执行）；串行队列 [104,105] 按序；`bad_difficulty`(99)；`unsupported_kind:menu_pause`；`not_in_level_up`（升级页外 index 同码）；断连在途 `menu_start_run` → `link_lost`（ref=112，重连后重发仍 `link_lost`；开局为断连前副作用已发生）；对局开始证据（wave=1 combat）；`level_up` payload（wave=1，4 项）；`menu_pick_upgrade` index=1 ack ok + 差分确认。
  - `mode=start` `t06_session5_start.log`（in_run_scene 修正后重跑）4/4：hello / 难度页 payload / `menu_start_run` ack ok 0.44s / wave=1 combat。
  - `mode=timeout` `t06_session6_timeout.log`（修正后重跑）3/3：同帧 Back 竞态下 `start_timeout` 第 1 次尝试即命中（`{'ok': False, 'error': 'start_timeout'}`）。
  - 最终构建（删除探针后重新部署，12 文件）冒烟 `t06_session7_smoke.log`：hello v2 + snapshot 57.8Hz，PASS。
  - gdlint：改动/新增 4 文件全部通过（既有 `shop_actions.gd` / `movement.gd` 两处历史告警未动）。
- 剩余限制：① `menu_pause` 未实现（按票据为可选；回 `unsupported_kind`）；② 选卡差分确认对「选项 id 不变、`can_pick` 翻转」的形态也判成功（session3 第 4 卡 false→true 即此形态），与协议「签名变化即确认」一致；③ `difficulty_timeout` / `pick_timeout` 未单独构造实机用例（超时机制由 `start_timeout` 覆盖）；④ 验证脚本 `t06_receiver.py` 为临时工具，不入仓（与 01–05 同约定）。
