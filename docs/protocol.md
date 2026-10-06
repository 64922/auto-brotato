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
  "protocol_version": 1,
  "mod_version": "0.2.0",
  "game_version": "1.1.15.4",
  "session_id": "4042ab31d9aaf1e4",
  "capabilities": { "snapshot_hz": 60, "move_analog": true }
}
```

### 2.2 `welcome`（agent → mod）

```json
{
  "protocol_version": 1,
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
| `shop_buy` | `slot: int` | 购买槽位；前置校验（在商店、槽位有效、未售出、金币足够）；等游戏信号回执 |
| `shop_sell` | `inv_kind: "item"\|"weapon"`、`index: int` | 仅武器可回收（游戏规则：`inv_kind=item` 返回 `not_discardable`）；差分验证 |
| `shop_reroll` | 无 | 免费刷新或金币足够；差分验证（报价/金币/次数） |
| `shop_lock` | `slot: int`, `locked: bool` | 读回校验 |
| `shop_leave` | 无 | 离开按钮可用时才允许 |

### 5.2 可靠性约定

1. **TTL**：`move` 超过 `action_ttl_ms`（默认 250ms）未更新 → 归零；断连立即归零。
2. **幂等账本**：mod 按 `ref` 缓存最近 256 条动作结果；重复 `ref` 只重发 `ack` 不重执行；在途/排队的同 `ref` 忽略重复触发。
3. **串行执行**：商店类动作不并发；在途/排队时新动作入队（上限 16，超出回 `busy`）。
4. **超时**：购买 1000ms、卖出 1500ms、刷新 700ms；超时回 `buy_timeout` / `sell_failed` / `reroll_failed`。agent 侧另有 2s 未回执判定失败（双保险）。
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
| `unsupported_kind:<kind>` | 所有 | 未知动作（含尚未实现的 `menu_*`） |
| `not_in_shop` | shop_* | 当前不在商店 |
| `bad_slot` / `slot_sold` | 买卖/锁定 | 槽位越界 / 已售出 |
| `bad_price` / `insufficient_gold` | 购买/刷新 | 价格异常 / 金币不足 |
| `buy_button_missing` | 购买 | 找不到购买按钮 |
| `bad_inv_kind` / `not_discardable` / `bad_index` | 卖出 | 类型非法 / 道具不可卖 / 索引越界 |
| `popup_missing` | 卖出 | 找不到物品弹窗 |
| `reroll_button_missing` | 刷新 | 找不到刷新按钮 |
| `bad_locked` / `not_lockable` / `lock_button_missing` / `lock_failed` | 锁定 | 参数/物品/按钮/校验失败 |
| `cannot_leave` | 离开 | 离开按钮不可用 |
| `busy` | shop_* | 队列已满 |
| `buy_timeout` / `sell_failed` / `reroll_failed` | shop_* | 超时或差分验证失败 |
| `link_lost` | shop_* | 断连导致在途动作终止 |

> mod 日志中的断开原因（`welcome_timeout`、`connection_lost`、`write_error` 等）不进入协议，仅用于排查。

## 7. v2 增量规约（规划，票据 05/06 实现后固化）

### 7.1 `menu` 消息（mod → agent）

- 与 `shop` 相同的推送策略：内容变化时推送 + 1s 心跳；不在任何菜单时**不发送**（agent 侧以静默 + 快照状态判定）。
- `payload.phase` ∈ `difficulty_select` | `level_up` | `run_end`。

#### 7.1.1 `phase=difficulty_select`（难度选择页）

| 字段 | 说明 |
| --- | --- |
| `character.id` | 英雄 ID（如 `character_ranger`） |
| `weapons` | 初始武器 `[{id, tier}]` |
| `difficulty.selected` | 当前滑杆值 |
| `difficulty.max_selectable` | 当前英雄可选上限（运行时读取，不硬编码） |
| `difficulty.unlocked` | 可选值集合（如 `[0,1]`） |
| `modes` | `{endless, ban, zone_is_random, zone_selected, coop}` 等开关现值 |
| `can_start` | 是否可开始对局 |

> 字段名与来源以票据 04 的实机 spike 结论为准；本表在 spike 后修订。

#### 7.1.2 `phase=level_up`（波间升级选卡）

| 字段 | 说明 |
| --- | --- |
| `wave` | 当前波次 |
| `options` | 升级卡 `[{slot, kind, id, tier}]`（结构以票据 04 为准） |

#### 7.1.3 `phase=run_end`（终局）

| 字段 | 说明 |
| --- | --- |
| `result` | `victory` \| `defeat` |
| `wave` | 到达波次 |
| `stats` | 结算统计（金币/击杀等，字段以票据 04 为准） |

### 7.2 `menu_*` 动作（agent → mod）

| kind | 参数 | 语义与约束 |
| --- | --- | --- |
| `menu_set_difficulty` | `value: int` | 将难度滑杆设为 `value`（按差值步进加减按钮或等价路径）；不在难度页 / 超出可选范围 → `not_in_difficulty_select` / `bad_difficulty`；设置后读回校验 |
| `menu_start_run` | 无 | 难度页开始对局（`can_start` 为真，否则 `cannot_start`）；经游戏开始按钮信号 |
| `menu_pick_upgrade` | `index: int` | 升级选卡（`level_up` phase）；索引越界 → `bad_index` |
| `menu_pause`（可选） | 无 | 注入游戏暂停（P1 可选增强，见架构 §13 未决） |

新增错误码（v2）：`not_in_difficulty_select`、`bad_difficulty`、`cannot_start`、`not_in_level_up`、`bad_index`。

### 7.3 版本流程

- `PROTOCOL_VERSION` 改为 2；`hello/welcome` 双侧校验并拒绝不匹配会话（§2.3）。
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
- 进度：`RunData.current_wave`、`RunData.get_player_weapons_ref(0)`、`RunData.get_player_items(0)`、`RunData.get_player_gold(0)`、`RunData.get_player_count()`
- 属性：`Utils.get_stat(Keys.generate_hash(name), 0)`

**商店（shop_observation.gd / shop_actions.gd）**

- 商店场景根：`fill_shop_items` + `on_shop_item_bought` 方法判定；`%ShopItemsContainer`、`%GearContainer`、`%RerollButton`、`%GoButton`、`%ItemPopup`
- 槽位节点：`set_shop_item`、`item_data`、`active`、`value`、`locked`、`%BuyButton`、`%LockButton`
- 背包容器：`ItemsContainer/WeaponsContainer → ScrollSizeContainer/ScrollContainer/Elements`、`item`、`current_number`
- 商店状态：`_reroll_price`、`_reroll_count`、`_free_rerolls`
- 游戏信号：`shop_item_bought`、`shop_item_insufficient_currency`、`ItemPopup.item_discard_button_pressed`

**菜单（P1 新增，待票据 04 实测）**

- 难度页：`DifficultySelection` 场景、`DifficultySliderContainer`、`DecreaseDifficultyButton` / `IncreaseDifficultyButton`、`difficulty_selected_value`、`difficulty_unlocked`、开始按钮
- 升级页 / 终局页：待实测
