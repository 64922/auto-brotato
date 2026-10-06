# IPC 协议规约：AutoBrotato mod ⇄ Python agent

> 状态：v1 为既有 mod 0.2.0 实机验证过的现状（本文档从源码整理）；v2 为规划增量（票据 05/06 实现）。协议随 mod 与 agent 一起发布（单客户端，不做双版本兼容，ADR-0009）。

---

## 1. 传输与信封

- 传输：TCP `127.0.0.1:37650`
- 方向：**mod 是客户端（主动连出），agent 是服务端（监听该端口）**
- 编码：UTF-8，NDJSON（每行一个 JSON 对象，`\n` 分隔）
- 信封字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `v` | int | 协议版本（v1 现状；v2 见 §7） |
| `seq` | int | 发送方单调递增序号（从 0 起） |
| `ts` | float | Unix 时间戳（秒，含毫秒小数） |
| `type` | string | 消息类型 |
| `ref` | 任意/空 | 仅动作与回执使用；动作的唯一标识（同 ref 幂等） |
| `payload` | object | 消息体 |

- 连接生命周期（mod 侧状态机）：`disconnected → connecting → handshaking → ready`；会话被拒为 `rejected`（停止重连）。
- 重连：指数退避 2s → 10s；`welcome` 握手超时 10s；断开原因仅记录在 mod 日志（`%APPDATA%\Brotato\logs`），不通过协议传递。
- 兜底：`ready` 后超过 2s 未收到任何 agent 消息 → mod 判定链路失效，移动注入归零（安全停住）。

## 2. 握手与版本

### 2.1 `hello`（mod → agent，连接建立后立即发送）

```json
{
  "protocol_version": 2,
  "mod_version": "0.2.0",
  "game_version": "1.1.15.4",
  "session_id": "4042ab31d9aaf1e4",
  "capabilities": { "snapshot_hz": 60, "move_analog": true }
}
```

### 2.2 `welcome`（agent → mod）

```json
{
  "protocol_version": 2,
  "session_id": "4042ab31d9aaf1e4",
  "config": { "snapshot_hz": 60, "debug_overlay": false, "action_ttl_ms": 250 }
}
```

| 配置 | 范围 | 说明 |
| --- | --- | --- |
| `snapshot_hz` | 1–120（clamp） | 快照上送频率 |
| `debug_overlay` | bool | 是否开启调试叠加层 |
| `action_ttl_ms` | 50–1000（clamp） | 动作有效期（TTL） |

### 2.3 版本校验（v2 起强制）

- v1 现状：mod 收到 `welcome` 即进入 `ready`，不做版本比对。
- v2 起：agent 在收到 `hello` 后校验 `protocol_version == PROTOCOL_VERSION` 且 `game_version == 锁定版本（1.1.15.4，ADR-0005）`；不匹配 → 回 `error` 并断开，mod 置 `rejected`、停止重连，终端给出明确提示。

## 3. 消息清单（mod → agent）

| type | 频率 | 说明 |
| --- | --- | --- |
| `hello` | 每次连接一次 | 见 §2.1 |
| `snapshot` | `snapshot_hz`（默认 60Hz） | 战斗观测，见 §3.1 |
| `shop` | 内容变化时 + 1s 心跳 | 商店观测，见 §3.2；未打开时**不发送**（靠 1.5s 静默判定关闭） |
| `menu`【v2】 | 内容变化时 + 1s 心跳 | 菜单观测（难度页/升级页/终局），见 §7 |
| `event` | 事件发生时 | 目前唯一事件：`purchase_done`（先于该动作的 `ack` 发送） |
| `ack` | 每个动作一条 | `{"ok": true}` 或 `{"ok": false, "error": "<code>"}`；`ref` 回显 |
| `pong` | 收到 `ping` 时 | 心跳应答 |

### 3.1 `snapshot` payload（60Hz）

> 场景未就绪的类别给 `null`/空数组，字段不缺；数值一律 JSON number；`id` 为游戏内实体 ID 或字符串 ID。

| 字段 | 说明 |
| --- | --- |
| `t` | mod 侧时间（秒，`OS.get_ticks_msec()/1000`） |
| `wave` | `{index, phase: "combat"\|"ended", time_left, time_total}` 或 `null` |
| `player` | `{pos:[x,y], vel:[x,y], hp, max_hp, alive, invuln, speed, armor, dodge, lifesteal, regen}` 或 `null` |
| `weapons` | 玩家武器冷却状态：`[{id, tier, cooldown_pct}]`（`cooldown_pct = 当前冷却 / 总冷却`） |
| `enemies` | `[{id:int, type, pos, vel, hp_pct, radius, elite, boss}]`；900px 裁剪，上限 300，最近优先 |
| `projectiles` | `[{id:int, pos, vel, radius, friendly:bool, ttl}]`；900px 裁剪，上限 400，最近优先 |
| `pickups` | `[{kind: "material"\|"consumable", pos}]` |
| `hazards` | `[{kind: "landmine", pos, radius}]` |
| `arena` | 场地矩形 `{min:[x,y], max:[x,y]}` 或 `null` |
| `inventory` | `{weapons:[{slot,id,tier}], items:[{id,count}]}`（500ms 缓存） |
| `stats` | 见 §3.1.1 |
| `economy` | `{gold, materials_this_wave}` |
| `fps` | 游戏帧率 |
| `truncated` | 敌人或弹幕达到上限被截断时为 `true`（决策需感知盲区，见架构 §13 R4） |

#### 3.1.1 `stats` 字段（14 项，缺省 0.0）

`melee_damage, ranged_damage, attack_speed, crit_chance, harvesting, luck, engineering, range, max_hp, hp_regen, lifesteal, armor, dodge, speed`

### 3.2 `shop` payload（商店打开期间）

| 字段 | 说明 |
| --- | --- |
| `wave_next` | 下一波序号（`RunData.current_wave + 1`） |
| `gold` | 当前金币 |
| `slots` | `[{slot, kind: "item"\|"weapon", id, tier, price, sold, locked}]` |
| `inventory` | `{weapons:[{slot,id,tier}], items:[{id,count}]}` |
| `stats` | 同 §3.1.1 |
| `reroll` | `{cost, count}`（`cost` 为 -1 表示读不到） |
| `can_leave` | 离开按钮可用 |

### 3.3 `event`

```json
{ "name": "purchase_done", "data": { "slot": 2, "id": "item_id", "price": 15 } }
```

购买成功时发送，先于对应的 `ack`。

## 4. 消息清单（agent → mod）

| type | 说明 |
| --- | --- |
| `welcome` | 见 §2.2 |
| `ping` | 心跳；mod 回 `pong` |
| `action` | 动作指令：`payload.kind` + 参数，见 §5 |

## 5. 动作语义与可靠性

### 5.1 动作定义（v1 现状）

| kind | 参数 | 语义与约束 |
| --- | --- | --- |
| `move` | `vector: [dx, dy]` | 模长 ≤1（超界归一化）；分量必须是数字（NaN 拒绝）；按 TTL 生效 |
| `debug_overlay` | `enabled: bool` | 调试叠加层开关 |
| `debug_export_knowledge`【票据 11】 | 无 | 调试动作：从游戏运行时资源（`/root/ItemService` / `/root/ChallengeService`）导出知识库 JSON 到 `user://auto_brotato_knowledge/`，只读、不碰游戏状态、与 TTL 无关。成功 ack 附带 `dir`（绝对路径）、`files`（每个文件 `name/sha256/bytes`）、`data_versions`（每个文件 data_version）、`counts`（各类条目数）；失败见 §6 |
| `shop_buy` | `slot: int` | 购买槽位；前置校验（在商店、槽位有效、未售出、金币足够）；等游戏信号回执 |
| `shop_sell` | `inv_kind: "item"\|"weapon"`、`index: int` | 仅武器可回收（游戏规则：`inv_kind=item` 返回 `not_discardable`）；差分验证 |
| `shop_reroll` | 无 | 免费刷新或金币足够；差分验证（报价/金币/次数） |
| `shop_lock` | `slot: int`, `locked: bool` | 读回校验 |
| `shop_leave` | 无 | 离开按钮可用时才允许 |

### 5.2 可靠性约定

1. **TTL**：`move` 超过 `action_ttl_ms`（默认 250ms）未更新 → 归零；断连立即归零。
2. **幂等账本**：mod 按 `ref` 缓存最近 256 条动作结果；重复 `ref` 只重发 `ack` 不重执行；在途/排队的同 `ref` 忽略重复触发。
3. **串行执行**：商店类动作与菜单类动作各自不并发（`shop_*` 队列与 `menu_*` 队列独立）；在途/排队时新动作入队（上限 16，超出回 `busy`）。
4. **超时**：购买 1000ms、卖出 1500ms、刷新 700ms；设置难度 700ms、开始对局 1500ms、升级选卡 1500ms；超时回 `buy_timeout` / `sell_failed` / `reroll_failed` / `difficulty_timeout` / `start_timeout` / `pick_timeout`。agent 侧另有 2s 未回执判定失败（双保险）。
5. **断链记账**：断连时在途动作记 `link_lost`；排队动作直接丢弃（无副作用）；重连后旧 `ref` 不会再次执行。

### 5.3 动作回执时序

```
agent ── action(move, ref=17) ──▶ mod
agent ◀── ack({ok:true}, ref=17) ─ mod
```

购买类动作的完整时序（事件先于 ack）：

```
agent ── action(shop_buy, ref=18) ──▶ mod ──(UI 信号)──▶ 游戏逻辑
agent ◀── event(purchase_done) ──────── mod
agent ◀── ack({ok:true}, ref=18) ────── mod
```

## 6. 错误码表（`ack.error`）

| error | 来源 | 含义 |
| --- | --- | --- |
| `bad_vector` | move | 参数不是长度为 2 的数字数组（含 NaN） |
| `bad_enabled` | debug_overlay | `enabled` 非 bool |
| `unsupported_kind:<kind>` | 所有 | 未知动作（含未实现的 `menu_pause`） |
| `not_in_shop` | shop_* | 当前不在商店 |
| `bad_slot` / `slot_sold` | 买卖/锁定 | 槽位越界 / 已售出 |
| `bad_price` / `insufficient_gold` | 购买/刷新 | 价格异常 / 金币不足 |
| `buy_button_missing` | 购买 | 找不到购买按钮 |
| `bad_inv_kind` / `not_discardable` / `bad_index` | 卖出 / 选卡 | 类型非法 / 道具不可卖 / 索引越界或卡片不可选 |
| `popup_missing` | 卖出 | 找不到物品弹窗 |
| `reroll_button_missing` | 刷新 | 找不到刷新按钮 |
| `bad_locked` / `not_lockable` / `lock_button_missing` / `lock_failed` | 锁定 | 参数/物品/按钮/校验失败 |
| `cannot_leave` | 离开 | 离开按钮不可用 |
| `not_in_difficulty_select` | menu_* | 当前不在难度选择页 |
| `bad_difficulty` | menu_set_difficulty | 目标难度不存在或未解锁 |
| `cannot_start` | menu_start_run | 无选中元素或选中元素锁定 |
| `not_in_level_up` | menu_pick_upgrade | 当前不在升级选卡页 |
| `difficulty_timeout` / `start_timeout` / `pick_timeout` | menu_* | 设置难度读回 / 开局信号 / 选卡差分超时 |
| `busy` | shop_* / menu_* | 队列已满 |
| `item_service_missing` / `mkdir_failed:<n>` / `write_failed:<file>` / `export_failed` | debug_export_knowledge | 游戏单例未就绪 / 创建输出目录失败 / 写文件失败 / 其他导出失败（票据 11） |
| `buy_timeout` / `sell_failed` / `reroll_failed` | shop_* | 超时或差分验证失败 |
| `link_lost` | shop_* / menu_* | 断连导致在途动作终止 |

> mod 日志中的断开原因（`welcome_timeout`、`connection_lost`、`write_error` 等）不进入协议，仅用于排查。

## 7. v2 增量规约（票据 04 实机定型；票据 05/06 实现后固化）

### 7.1 `menu` 消息（mod → agent）

- 与 `shop` 相同的推送策略：内容变化时推送 + 1s 心跳；不在任何菜单时**不发送**（agent 侧以静默 + 快照状态判定）。
- `payload.phase` ∈ `difficulty_select` | `level_up` | `run_end`。
- 实现状态：mod 侧已实现全部三个 phase 与全部 `menu_*` 动作（票据 05：难度页/终局 `menu_observation.gd`；票据 06：升级页 `level_up_observation.gd` + `menu_actions.gd`），上报协议 v2；agent 侧 hello 校验/welcome 属票据 07。

#### 7.1.1 `phase=difficulty_select`（难度选择页）

> 票据 04 实机结论 + 票据 06 反编译 `difficulty_selection.gd` 修正：本版本（1.1.15.4）难度选择**不是**
> `zone_ui` 滑杆，而是与选人/选武器一致的 `base_selection` 式 **InventoryElement 列表**。元素
> `pressed`（`_on_element_pressed`）对任意**非 special**元素立即置 `RunData.current_difficulty` 并
> `change_scene(game_scene)` 开局（**没有二次确认**，锁定/随机元素 `is_special=true`，pressed 被忽略）；
> 焦点移动（`element_focused`）只更新高亮面板与 `_latest_focused_element`，不影响开局。

路径前缀：`/root/DifficultySelection`（场景 `res://ui/menus/run/difficulty_selection/difficulty_selection.tscn`）。

| 字段 | 说明（实机来源） |
| --- | --- |
| `character.id` | `MarginContainer/VBoxContainer/DescriptionContainer/CharacterPanel.item_data.my_id`（`character_panel_ui.gd`，如 `character_ranger`） |
| `weapons` | `.../DescriptionContainer/WeaponPanel.item_data`（`item_panel_ui.gd`）：`{my_id:"weapon_pistol_1", weapon_id:"weapon_pistol", tier, ...}` |
| `difficulty.options` | `.../ScrollContainer/Inventories/Inventory1` 下各 `InventoryElement`（`inventory_element.gd`）的 `item`（`difficulty_data.gd`）：`my_id`=`difficulty_0..6`、`name`、`tier`、`value`、`is_locked`、`unlocked_by_default`（实测仅 difficulty_0=true）；元素自身 `current_number`（实测 D0=1、其余=0，语义未定，仅记录不依赖） |
| `difficulty.selected` | 当前预选/高亮元素：根脚本变量 `_latest_focused_element[0]`（实机进入页面默认聚焦 `difficulty_0` 元素；无人选中时为 `null`）。注意：焦点仅决定高亮/本字段，**任意非 special 元素一次 `pressed` 即开局**（无二次确认） |
| `difficulty.displayed` | 根脚本变量 `displayed_elements[0]`（玩家 0 的难度 `difficulty_data` 列表）的 `my_id` 数组 |
| `modes` | **权威状态在 `RunData`（票据 05 实机修正）**：`endless`=`RunData.is_endless_run`、`ban`=`RunData.is_ban_mode_active`、`coop`=`RunData.is_coop_run`（bool）；另附难度页根变量 `add_random_element` / `enable_coop_panels`（语义未证实，仅记录）。开关 UI 位于选人页 `RunOptionsPanel`（`EndlessButton` / `BanButton` / `CoopButton` / `ZoneSelectionButton`），离开选人页即释放，不可在难度页读取；zone 选择暂不输出 |
| `can_start` | 当前焦点元素存在且 `is_special != true`（反编译 `_on_element_pressed` 的接受条件；锁定/随机元素均 special）。实机验证：`is_special=true` 的 pressed 为**完全空操作**——`_latest_focused_element` 与 `_has_player_selected` 均不变、未开局） |
| （候选）`difficulty.max_unlocked` | `RunData.difficulty_unlocked`（int，usage=8192）：实测 D0 局中与终局恒为 `-1`，语义未证实，**暂不作为读取来源**，仅登记候选 |

关键信号（实机 `conns` 证据）：

- `Inventory1`（`inventory.gd`）：`element_pressed -> DifficultySelection._on_element_pressed`（开局）、`element_focused -> DifficultySelection._on_element_focused`（更新 `_latest_focused_element` 与高亮面板）、`element_hovered` 同根。
- `BackButton.pressed -> DifficultySelection._on_BackButton_pressed -> _go_back`：置 `RunData.menu_selection_back=true` 并切回选武器/选人页。注意该标志会被目标菜单页 `_ready` 复位（`weapon_selection.gd`/`character_selection.gd`），**不能跨帧用于「返回 vs 开局」判定**；票据 06 以「难度页离场且 `/root/Main` 就位」判开局。
- **无 `run_started` 信号**（票据 04 记录有误；该字符串只是 `ProgressData` 统计键名）：`difficulty_selection.gd` 直接 `get_tree().change_scene(MenuData.game_scene)`，无开局信号可订阅。

#### 7.1.2 `phase=level_up`（波间升级选卡）

> 票据 04 实机结论：UI 常驻 `/root/Main/UI/UpgradesUI`（`upgrades_ui.tscn`，`upgrades_ui.gd`），
> 升级时显示；单人为 `UpgradesUIPlayerContainer1`（`upgrades_ui_player_container.gd`），
> 卡片 4 张（`UpgradeUI`、`UpgradeUI2..4`）。

路径前缀：`/root/Main/UI/UpgradesUI/MarginContainer/VBoxContainer/HBoxContainer2/UpgradesUIPlayerContainer{player}`（player=1..4）。

| 字段 | 说明（实机来源） |
| --- | --- |
| `wave` | 页面自身不带波次；用既有 `RunData.current_wave`（§8） |
| `options[].slot` | `UpgradesContainer/HBoxContainer/UpgradeUI{,2,3,4}` 的序号 1..4 |
| `options[].kind` | 卡片数据类型：`upgrade`（`upgrade_data.gd`）/ `weapon`（含 `weapon_id`）/ `item`；读不到数据给 `""` |
| `options[].id` | 卡片数据 `my_id`（`upgrade_data.gd`，如 `upgrade_percent_damage_3`）。读取双路径：卡片脚本变量 `upgrade_data`（`upgrade_ui.gd`）优先，回退 `MarginContainer/VBoxContainer/UpgradeDescription.item`（实机 dump：`item_description.gd` 的 `item` 即 `upgrade_data` 资源） |
| `options[].tier` | 同上 `upgrade_data.tier`（int） |
| `options[].can_pick` | 卡片 `MarginContainer/VBoxContainer/ChooseButton`（`my_menu_button.gd`，`text=MENU_CHOOSE`）：卡片与按钮均在屏幕上且 `disabled != true` |
| `player` | 容器 `UpgradesUIPlayerContainer{player+1}` 对应的 `player_index`（单人=0） |

未显示/无数据的卡片仍占位输出：`kind=""`、`id=""`、`tier=0`、`can_pick=false`。

关键信号（实机 `conns` 证据）：

- 卡片 `choose_button_pressed -> UpgradesUIPlayerContainer._on_choose_button_pressed`；卡片内 `ChooseButton.pressed -> UpgradeUI._on_ChooseButton_pressed`。
- 容器 → 根：`choose_button_pressed`、`item_take_button_pressed`、`item_discard_button_pressed`、`item_ban_button_pressed`。
- 根 → `/root/Main`：`upgrade_selected`、`consumable_selected`、`item_take_button_pressed`、`item_discard_button_pressed`、`item_ban_button_pressed`。
- 同页可能同时存在道具箱（`ItemsContainer/.../{TakeButton,DiscardButton,BanButton}`，`_item_data`/`_consumable_data`），本票只做取证不做行为设计。

#### 7.1.3 `phase=run_end`（终局）

> 票据 04 实机结论：`/root/EndRun`（`end_run.tscn`，`end_run.gd`；文案在 `base_end_run.gd`）。
> 票据 04 取得两份**战败**样本；票据 05 补齐**胜利**样本（`%Title.text="胜利 - 碰撞区域"`，波 20，
> `RunData.run_won=true`）。

路径前缀：`/root/EndRun`。

| 字段 | 说明（实机来源） |
| --- | --- |
| `result` | 结构化：`RunData.run_won`（bool）映射为 `"victory"` / `"defeat"`；读不到给 `null`。展示文本见 `title` |
| `title` | 展示文本 `%Title.text`（实测 `战败 - 碰撞区域` / `胜利 - 碰撞区域`，本地化含地图名） |
| `wave` | 结构化：`RunData.current_wave`（实测终局=6 / 10 / 20，与 `%RunInfo.text="第N波 - 危险X"` 一致）；RunData 不可读时 `null` |
| `stats` | `MarginContainer/VBoxContainer/PanelContainer/HBoxContainer/StatsContainer`（`stats_container.gd`）子树各格子的 `key` → `Value` Label 文本（字符串原样，实测 40 项，含 `CURRENT_LEVEL` / `STAT_*` / 次要属性） |

关键信号（实机 `conns` 证据）：

- `MarginContainer/VBoxContainer/HBoxContainer3/RestartButton`（`text=MENU_RESTART`）、`NewRunButton`（`MENU_NEW_RUN`）、`ExitButton`（`MENU_RETURN_MAIN`）：`pressed -> EndRun._on_*Button_pressed`。
- 终局页允许 Agent 明确不做任何操作（ADR-0003 单局止于终局）。

### 7.2 `menu_*` 动作（agent → mod）

| kind | 参数 | 语义与约束（票据 04 实机修订） |
| --- | --- | --- |
| `menu_set_difficulty` | `value: int` | **无滑杆**。实现=对 `Inventory1` 中 `item.my_id == "difficulty_<value>"` 的 InventoryElement 调用 `grab_focus()`（游戏 `focus_element_index` 同款焦点路径，**不按 pressed**）。约束：目标须存在且 `item.is_locked == false`，否则 `bad_difficulty`（实机验证：锁定元素 `is_special=true`，其 `pressed` 被忽略，无副作用）；**若目标已是当前选中元素则空操作**（`pressed` 会让游戏立即开局，属 `menu_start_run` 语义，避免副作用）；设置后读回 `_latest_focused_element[0]` 校验，700ms 未读回 → `difficulty_timeout` |
| `menu_start_run` | 无 | 对当前焦点元素（`_latest_focused_element[0]`）发 `pressed`：游戏 `_on_element_pressed` 用**被按元素**的值立即 `change_scene(main.tscn)` 开局（无需二次确认）。无焦点元素/`can_start=false` → `cannot_start`；成功判定=难度页离场且对局场景 `/root/Main` 已就位（经 BackButton 返回时进入选武器/选人页，不会出现 `Main`），1500ms 未完成 → `start_timeout`。注意：进入难度页默认聚焦 `difficulty_0` |
| `menu_pick_upgrade` | `index: int` | 按下 `UpgradesUIPlayerContainer{player}.UpgradesContainer/HBoxContainer/` 第 `index` 张卡片的 `ChooseButton`（index=1..4）。**命名注意**：index=1 → `UpgradeUI`（无后缀），index=2..4 → `UpgradeUI2..4`；索引越界/卡片不可选（不可见或按钮禁用）→ `bad_index`；按差分（升级页 options 签名变化或页面关闭）确认成功，1500ms 未变化 → `pick_timeout` |
| `menu_pause`（可选） | 无 | **未支持**（票据 06 未实现，架构 §13 未决）：`ack{ok:false, error:"unsupported_kind:menu_pause"}` |

`menu_*` 动作与 `shop_*` 相同：幂等账本（同 `ref` 只重发 ack，不重执行）、本模块内串行（在途时入队，上限 16 → `busy`）、断连时在途动作记 `link_lost`、排队动作丢弃。新增错误码（v2）：`not_in_difficulty_select`、`bad_difficulty`、`cannot_start`、`not_in_level_up`、`bad_index`、`difficulty_timeout`、`start_timeout`、`pick_timeout`（另复用通用 `busy` / `unsupported_kind:<kind>` / `link_lost`）。

### 7.3 版本流程

- `PROTOCOL_VERSION` 改为 2（mod 侧随票据 05 落地）；版本校验由 agent 在收到 `hello` 后执行（§2.3，agent 侧实现属票据 07），mod 收到 `rejected` 后停止重连（ADR-0009）。
- 不保留 v1 兼容路径；旧 mod 与新 agent 不相连（mod 被拒后停止重连，需部署匹配版本）。

## 8. 附录：mod 依赖的游戏内部字段清单（脆弱点）

> 游戏更新后按本清单逐一验证（冒烟票据 01/08）。所有访问均经 `get()/call()/has_method()`。

**观测（observation.gd）**

- `Main` 节点、`WaveTimer`；`Main._entity_spawner`
- spawner：`players` / `_players`、`enemies`、`structures`（`Landmine` 判定）
- 实体：`global_position`、`visible`、`dead`、`current_stats.health`、`max_stats.health`、`get_instance_id()`、`enemy_id`、`is_elite`、`_collision`（半径）
- 玩家：`current_weapons`、`_current_cooldown`、`weapon_id`、`tier`、`_invincibility_timer`
- 弹幕：`Main._player_projectiles` / `_enemy_projectiles`、`velocity`、`_time_until_max_range`、`_hitbox`
- 掉落：`Main._active_golds`、`Main._consumables`、`already_picked_up`
- 场地：`ZoneService.current_zone_rect`
- 进度：`RunData.current_wave`、`RunData.current_zone`、`RunData.run_won`（终局结构化胜负）、`RunData.difficulty_unlocked`（候选，语义未证实）、`RunData.get_player_weapons_ref(0)`、`RunData.get_player_items(0)`、`RunData.get_player_gold(0)`、`RunData.get_player_count()`
- 属性：`Utils.get_stat(Keys.generate_hash(name), 0)`

**商店（shop_observation.gd / shop_actions.gd）**

- 商店场景根：`fill_shop_items` + `on_shop_item_bought` 方法判定；`%ShopItemsContainer`、`%GearContainer`、`%RerollButton`、`%GoButton`、`%ItemPopup`
- 槽位节点：`set_shop_item`、`item_data`、`active`、`value`、`locked`、`%BuyButton`、`%LockButton`
- 背包容器：`ItemsContainer/WeaponsContainer → ScrollSizeContainer/ScrollContainer/Elements`、`item`、`current_number`
- 商店状态：`_reroll_price`、`_reroll_count`、`_free_rerolls`
- 游戏信号：`shop_item_bought`、`shop_item_insufficient_currency`、`ItemPopup.item_discard_button_pressed`

**菜单（P1 新增，票据 04 实机记录）**

- 难度页：`/root/DifficultySelection`（`difficulty_selection.gd`）——根变量 `displayed_elements` / `_has_player_selected` / `_latest_focused_element` / `_inventory1..4` / `_panel1..4` / `difficulty_selected` / `_selections_completed_delay`(0.8)；`.../ScrollContainer/Inventories/Inventory1`（`inventory.gd`，信号 `element_pressed`/`element_focused`/`element_hovered`）下 `InventoryElement`（`inventory_element.gd`）：`item`（`difficulty_data.gd`：`my_id`/`name`/`tier`/`value`/`is_locked`/`unlocked_by_default`）、`current_number`、`is_random`、`is_special`；`CharacterPanel.item_data`（`character_data.gd`）、`WeaponPanel.item_data`（`weapon_data.gd`）；`BackButton.pressed -> _go_back`（置 `RunData.menu_selection_back=true`）；**无 `run_started` 信号**，开局即 `_on_element_pressed` 内 `change_scene(game_scene)`（票据 06 反编译修正）。
- 升级页：`/root/Main/UI/UpgradesUI`（`upgrades_ui.gd`）——`_player_container1..4` / `_upgrades_to_process` / `_showing_option` / `_player_is_choosing`；容器 `UpgradesUIPlayerContainer{1..4}`（`upgrades_ui_player_container.gd`）——`player_index` / `_upgrade_ui_1..4` / `_reroll_button` / `_take_button` / `_discard_button` / `_ban_button` / `_item_data` / `_consumable_data`；卡片 `UpgradeUI{,2,3,4}`（`upgrade_ui.gd`）——`upgrade_data`（`upgrade_data.gd`）、`button`；按钮 `MarginContainer/VBoxContainer/ChooseButton`（`my_menu_button.gd`）。
- 终局页：`/root/EndRun`（`end_run.gd`）——`_title` / `_run_info` / `_restart_button` / `_new_run_button` / `_exit_button`；文案常量在 `base_end_run.gd`（`RUN_WON` / `RUN_LOST`）；统计 `StatsContainer`（`stats_container.gd`）。
- 引擎注意：本构建（Brotato 1.1.15.4 自定义 Godot 3.7）脚本变量的 `usage` 标记为 `0x2000`（非标准 Godot 的 `4096`）；以 `get_property_list()` 反射脚本变量时需同时识别两种标记（ADR-0006）。
