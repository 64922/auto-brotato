# 票据 15：发现道具页（消耗品弹窗）观测与动作

- Status: ready-for-agent
- Blocked by: 无
- 关联：票据 06（菜单动作）、07（会话状态机）、09（实机验收发现）、12（拿取/回收经济决策）

## 背景

票据 09 实机验收（2026-10-06，D0 游侠+SMG）发现：第 2 波拾取消耗品「植物」后，游戏弹出
「发现道具！」页面（拿取 / 回收 (+5)）并暂停对局；agent 未观测该页面、也没有对应动作，
走位动作无法推进流程，需人工点击「拿取」后才能继续（此后正常进入商店）。

**根因（反编译实机源码确认，`brotato_recovered/ui/menus/ingame/upgrades_ui*.gd`）**：
该页面复用 `UpgradesUI`/`UpgradesUIPlayerContainer1`：
- 道具模式时 `%ItemsContainer` 可见、`%UpgradesContainer` 隐藏（升级模式相反）；
- 按钮 `%TakeButton`（拿取）/`%DiscardButton`（回收）/`%BanButton`（禁用，仅挑战模式）；
- 容器脚本持有 `_item_data`（ItemParentData）与 `_consumable_data`，`%ItemDescription` 有 `item`；
- 点击后发 `item_take_button_pressed`/`item_discard_button_pressed` 信号，页面关闭。

现有 `mod/src/level_up_observation.gd` 只按升级卡判定（`UpgradesContainer` 卡片 + `ChooseButton`），
因此道具模式下 `pickable()` 全为 false、`options` 为 4 个空槽，agent 无动作可发。

## 目标

1. mod 观测（`level_up_observation.gd` 或独立 observer）：
   - 识别道具模式（`%ItemsContainer` 可见）；menu 快照新增/区分（如 `phase="item_found"`，
     `item`：id/名称/效果简述，`can_take`/`can_discard`）；
   - 复用既有快照推送节奏与新鲜度语义（票据 05/07）。
2. mod 动作：`menu_found_item`（参数 `choice="take"|"recycle"`），
   对齐票据 06 的幂等/串行/超时/差分验证（按下后以 ItemsContainer 隐藏或页面关闭验证）。
3. agent：`RunController` 识别 item_found 页面并自动选择（v1 默认 `take`；
   `recycle` 决策留给票据 12），打印摘要行，动作失败冷却重试（沿用 1.5s 冷却）。
4. 文档与测试：protocol.md（phase/动作）、菜单处理用例、回放分析支持该页面/动作。

## 验收

- [ ] 实机拾取消耗品弹窗出现后，agent 自动选择并继续，无人工介入
- [ ] 动作具备幂等/串行/超时与清晰错误码（与现有 menu_* 一致）
- [ ] 回放中可见该页面快照与动作回执；单测覆盖解析与重试路径

## 备注

- 截图证据：票据 09 实机第 2 波（植物，+3 生命再生 / -1% 生命窃取，93 材料）。
- `%BanButton` 仅挑战模式出现，v1 不处理。
- 触发不确定（依赖敌人掉落消耗品），实机验证需等待自然掉落或临时调高掉落。
- 票据 09 的占位对照不受影响（同一问题对两种策略一致，人工兜底即可）。
