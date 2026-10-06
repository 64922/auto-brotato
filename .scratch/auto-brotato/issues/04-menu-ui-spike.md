# 票据 04：菜单 UI 结构 spike（难度页 / 升级选卡 / 终局）

- Status: ready-for-human
- Blocked by: 01
- 关联：[protocol.md §7](../../../docs/protocol.md)、架构 §6.2、R3

## 背景

P1 的菜单观测与动作依赖游戏内部 UI 结构，但难度页（滑杆）、升级选卡、终局页的节点/字段/信号尚未实测。本票据先做一次性探针，产出可靠结论后再写正式代码。

## 目标

1. 写一个临时探针（mod 内调试代码或独立小脚本），在目标场景出现时把节点树/关键属性 dump 到日志或 IPC：
   - **难度选择页**（`ui/menus/run/difficulty_selection`）：
     - 场景根脚本类与方法；滑杆容器与加减按钮的节点路径；
     - 当前值、上限、解锁集合的读取方式（`difficulty_selected_value` / `difficulty_unlocked` / 存档字段）；
     - 开始按钮（`can_start` 的判定依据）；
     - 英雄与初始武器的读取路径（与 `character_selection` / `character_selection_inventory` 的关系）；
     - 模式开关（endless/ban/zone）在何处可读。
   - **升级选卡页**：场景/容器节点；卡片数据结构（id/tier/type 字段）；可用信号（选择动作等价路径）。
   - **终局页**：胜利/失败的判定信号或节点；结算统计的可读字段。
2. 人工配合：进入对应界面（选好英雄/武器停在难度页；打 1 波触发升级；正常/死亡各一次终局）。
3. 产出 `docs/protocol.md §7` 修订稿（字段定型）与本票据 Comments 结论。

## 验收

- [x] 三个场景的节点路径/字段/信号写入票据结论，含实机证据（日志片段）
- [x] 明确 `menu_set_difficulty` 的可行实现路径（按钮步进 vs 其它）
- [x] 不确定项全部列出（若无则写"无"）

## 备注

- 探针代码不进入正式 mod 发布；结论先行、实现后置（票据 05/06）。
- 若某读取路径不稳定，准备双路径（`%UniqueName` + 方法回退，ADR-0006）。

## Comments

- 2026-10-06：票据 04 实机 spike 完成；分支 `ticket/04-menu-ui-spike`（worktree）保留验收，未合并、未推送。
- 探针实现（一次性，票据 05/06 落地时删除）：`mod/src/spike_menu_probe.gd` + `spike_menu_probe_dump.gd`（反射 dump）+ `spike_menu_probe_nav.gd`（信号驱动导航）；运行中命令文件 `%APPDATA%\Brotato\auto_brotato_spike_cmd.json`（`dump`/`navigate`/`press`/`emit`/`props`）。
- 实机已验证全自动流程（session 3162）：main_menu(StartButton) → character_select（ranger 双击）→ weapon_select（pistol 双击）→ difficulty_select（difficulty_0 双击）→ 开局 → shop(GoButton) → level_up（ChooseButton 连按）→ … → run_end（战败，波 6/7）。合成输入不被游戏接收（同票据 02），全部导航走 `emit_signal("pressed")`。
- 结论 1：难度页——**不是 ZoneUI 滑杆**，是 `base_selection` 式 InventoryElement 列表。
  - 路径：`/root/DifficultySelection`（`difficulty_selection.tscn/.gd`）→ `MarginContainer/VBoxContainer/ScrollContainer/Inventories/Inventory1`（`inventory.gd`；信号 `element_pressed`/`element_focused`/`element_hovered` → 根 `_on_element_*`）；元素 `inventory_element.gd`：`item` = `difficulty_data.gd`（`my_id`=difficulty_0..6、`name`、`tier`、`value`、`is_locked`、`unlocked_by_default`）+ 元素 `current_number`（实测 D0=1、其余=0，语义未定）。
  - 根变量（实机 props 证据）：`displayed_elements`（7 个 difficulty_data）、`_has_player_selected=[false×4]`、`_latest_focused_element`（进入页面默认 = D0 元素）、`_selections_completed_delay=0.8`、`difficulty_selected=false`；根信号 `run_started`。
  - 行为实测：① 按下**锁定**元素（@635/D6，`is_locked=true`）完全空操作——focused 不变、未开局（证据 `ev_difficulty_locked.txt`）；② 对**已聚焦**元素再按一次即确认开局（D0 单击直接 `run_started`）。
  - 英雄/武器读取：`CharacterPanel.item_data`=`character_data`（`character_ranger`）、`WeaponPanel.item_data`=`weapon_data`（`weapon_pistol_1`/`weapon_pistol`）；`character_selection_inventory.gd` 仅含排序字符串（`abyssal_terrors`），与读取路径无关（回答票据问题）。
- 结论 2：升级页——常驻 `/root/Main/UI/UpgradesUI`（`upgrades_ui.gd`）；容器 `…/HBoxContainer2/UpgradesUIPlayerContainer1`（`player_index=0`）；卡片 `UpgradesContainer/HBoxContainer/UpgradeUI`（首个无后缀）、`UpgradeUI2..4`（`upgrade_ui.gd`：`upgrade_data.my_id`/`tier`、`button`）；按钮 `MarginContainer/VBoxContainer/ChooseButton`（`my_menu_button.gd`，`text=MENU_CHOOSE`）。信号链：`ChooseButton.pressed → UpgradeUI._on_ChooseButton_pressed → 容器 choose_button_pressed → UpgradesUI._on_choose_button_pressed`。连升多级会连续弹卡（导航不置完成、反复按）。
- 结论 3：终局页——`/root/EndRun`（`end_run.gd`）：`%Title` 实测 `战败 - 碰撞区域`、`%RunInfo` 实测 `第6波 - 危险0`；按钮 `…/HBoxContainer3/{RestartButton,NewRunButton,ExitButton}`（MENU_RESTART/NEW_RUN/RETURN_MAIN）；实机按下 `NewRunButton` 成功回到选人流程。**结构化来源**：`RunData.run_won=false`（战败）、`RunData.current_wave=6`、`RunData.current_zone=0`；`RunData.difficulty_unlocked=-1`（候选，语义未证实）。
- `menu_set_difficulty` 可行路径：按下 `Inventory1` 中 `item.my_id=="difficulty_<value>"` 的元素一次（`pressed` 即改选中）；目标已聚焦必须空操作（再按=确认开局）；目标 `is_locked` → 空操作，回 `bad_difficulty`；设置后读回 `_latest_focused_element[0]` 校验。`menu_start_run` = 对当前选中元素再按一次（注意进入页面默认已聚焦 D0）。与 protocol §7.2 修订一致。
- protocol.md §7 已整体修订：§7.1.1/7.1.2/7.1.3 字段表（含 `tier`、`current_number`、`unlocked_by_default`、`run_won`、`menu_pick_upgrade` 索引命名等），§8 附录补 `run_won`/`difficulty_unlocked`。
- 实机证据（本机，未入库）：spike 日志 `%APPDATA%\Brotato\auto_brotato_spike.log`（session 2762/3162）；抽取件 `%TEMP%\opencode\{ev_difficulty2.txt, ev_difficulty_locked.txt, ev_difficulty_props.txt, ev_rundata_start.txt, ev_runend_new.txt}`。
- 不确定项：① 难度「上限/存档字段」未证实（`difficulty_unlocked` 实测恒 -1）；已证实解锁判据仅 `item.is_locked` / `unlocked_by_default`；② `RUN_WON` 终局分支未实测（仅静态字符串）；③ coop 变体未测；④ 元素 `current_number` 语义未定；⑤ 模式开关（endless/ban/zone）只观察节点与信号，未验证状态语义。
- 环境注意（复现用）：本构建给脚本变量打的 `usage` 标记是 `0x2000`（非标准 4096），反射需双标记（ADR-0006 相关）；强杀游戏后 ModLoader 会把 `mod_user_profiles.json` 写空（"Mods are currently disabled"），下次启动前需从 `%APPDATA%\Brotato\backups\` 恢复。
- code-review 复核：Standards 硬项（探针单文件 >500 行）已修——拆分为 3 文件（321/293/253 行）；Spec 项已修（字段表补全、`menu_pick_upgrade` index=1→无后缀 `UpgradeUI`、`can_start` 由实测证据支撑）。遗留（judgement calls）：树遍历/重试模式在三个 spike 文件间重复——一次性代码，随票据 05/06 删除，不带入正式实现。
