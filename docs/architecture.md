# AutoBrotato 系统架构（开发主文档）

> 状态：2026-10-06 与用户逐条评审确认。后续设计变更必须更新本文件或新增 `docs/adr/`。
>
> 关联文档：[protocol.md](./protocol.md)（IPC 协议规约）、[strategy.md](./strategy.md)（决策引擎与策略设计）、[docs/adr/](./adr/)（架构决策记录）、[.scratch/auto-brotato/PRD.md](../.scratch/auto-brotato/PRD.md)（需求与任务票单）。

---

## 1. 目标

用户在 **Brotato** 中：

1. 手动进入游戏、手动选择英雄与初始武器，停留在**难度选择页**；
2. 系统检测到该页面后，读取英雄、初始武器、可选难度（按英雄/存档独立解锁）与模式开关，并在**终端**提示用户选择难度；
3. 用户输入任意**已解锁**难度（如 `D0`、`D3`），系统自动设置难度并进入对局；
4. 系统全自动、高水平地打完整局（第 1–20 波及所有商店、升级选卡），然后停止并在终端报告结果。

单机本地运行；不对游戏状态做任何直接修改（只经游戏正常输入与 UI 流程注入动作），不涉及联机与他人影响。

### 1.1 非目标

- 不自动选择英雄 / 初始武器（保留人工）。
- 不自动重试、不连刷多局（一局制，见 §4.5）。
- 首版不承诺全部英雄通关率；验收基准为**游侠 Ranger**（其余英雄"尽力而为"）。
- 不做在线学习 / 强化学习（ADR-0002）。

### 1.2 验收标准

在验收基准英雄与用户指定初始武器下：

> **D0 连续 10 局自动对局 ≥ 9 局通关**；除"输入难度"外全程无人工干预；不使用暂停、作弊或直接改游戏状态。

---

## 2. 背景与现状（事实勘察结论）

### 2.1 游戏

| 项 | 值 |
| --- | --- |
| 游戏 | Brotato 1.1.15.4（Steam app `1942280`，Godot 3.7 定制引擎） |
| 安装 | `E:\SteamLibrary\steamapps\common\Brotato` |
| 瞄准 | **自动瞄准默认开启**（`settings.manual_aim=false`）：武器自动瞄准最近敌人，机器人只需控制移动 |
| 难度页 | `ui/menus/run/difficulty_selection`，**滑杆式**（`DifficultySliderContainer` + 加减按钮，`difficulty_selected_value` / `difficulty_unlocked`） |
| 难度解锁 | 按英雄独立记录（存档 `difficulties_unlocked[character].max_selectable_difficulty`） |
| 升级 | 波间从若干升级项中选 1（资源 `items/upgrades/*`） |
| 存档 | `user://76561199368828270/`（`save_v3_1.json` / `run_v3_1.json`，游戏自带 `.bak`） |
| 语言 | 中文（`zh`） |

当前档值得注意的设置：`pause_on_focus_lost=false`（失焦不暂停，可后台运行）、`on_lost_focus=1`、`manual_aim=false`、`fullscreen=true`、`endless_mode_toggled=true`、`ban_mode_toggled=true`。系统会在难度页**读取并展示**这些开关，避免用户在非预期模式下开局（是否将其恢复为常规值由用户自行决定）。

### 2.2 既有资产（本系统的源码基线）

本机已有一套**实机验证过**的 AutoBrotato mod 0.2.0（最近一次运行日志 2026-10-06 11:27，hello/welcome 握手成功、快照上送正常）：

- 源码唯一副本（zip）：`E:\SteamLibrary\steamapps\workshop\content\1942280\3814007307\BrotatoPlayer-AutoBrotato-0.2.0.zip`
- 部署痕迹：`%APPDATA%\Brotato\auto_brotato_deploy.json`、`backups/`、`mod_user_profiles.json`
- 已有能力：
  - IPC：TCP NDJSON v1，端口 `37650`，hello/welcome 握手、心跳、指数退避重连、断线 TTL 兜底
  - 战斗观测 60Hz：玩家/武器/敌人/弹幕/掉落/地雷/场地/背包/属性/经济
  - `move` 注入：8 向数字 + PWM 占空比（实机验证；合成手柄模拟量事件在本引擎不可用）
  - 商店观测与 `shop_buy / shop_sell / shop_reroll / shop_lock / shop_leave`（走游戏 UI 信号、幂等、串行）
  - 调试叠加层（overlay）
- 缺口（本系统需补齐）：
  1. 菜单观测：难度页（英雄/武器/难度解锁/模式开关）、升级选卡页、终局页
  2. 菜单动作：设置难度滑杆、开始对局、选择升级
  3. Python 决策端（IPC 客户端、状态机、终端交互、决策引擎、回放）
  4. 工程文档与仓库（本文件集即补齐文档；源码入仓见票据 01）

> 原项目的 `architecture.md` 与票据体系已随源码丢失，mod 源码注释中引用的 `ADR-0006/0007`、`§4.x` 编号为历史引用；对应决策已在 [docs/adr/](./adr/) 中重建（0006、0007），编号保持稳定。

### 2.3 开发环境

- Windows / PowerShell 5.1；所有开发与运行在 conda 环境 **`brotato`**（Python 3.12.14，当前仅装 numpy）。
- 开发期联网（查资料/搜索）走用户代理 `http://127.0.0.1:7890`；系统运行本身不需要网络。

---

## 3. 总体架构

```
┌───────────────────────────── 游戏进程（单机） ─────────────────────────────┐
│ Brotato.exe + Godot ModLoader + AutoBrotato mod                            │
│  ├─ ipc_client.gd      TCP 客户端（主动连出 127.0.0.1:37650）               │
│  ├─ observation.gd     战斗观测（60Hz 采样）                                │
│  ├─ movement.gd        move 注入（Input，TTL 安全停住）                     │
│  ├─ shop_*.gd          商店观测 / 动作（游戏 UI 信号）                      │
│  └─ menu_*.gd【新增】  难度页/升级页/终局观测与动作（游戏 UI 信号）          │
└──────────────────────────────────┬─────────────────────────────────────────┘
                       TCP 127.0.0.1:37650 · NDJSON · 协议 v1 → v2
┌──────────────────────────────────┴─────────────────────────────────────────┐
│ Python agent（conda brotato）                                              │
│  ├─ ipc_server      监听端口、握手/版本校验、收 envelope、发 action          │
│  ├─ state           最新快照/商店/菜单视图 + 会话状态机                     │
│  ├─ decision        反射层（走位/避弹）· 战术层 · 经济层（商店/升级）        │
│  ├─ cli             终端交互（难度选择、status/stop/resume/报告）           │
│  └─ recorder        回放落盘（NDJSON：观测+动作+决策理由）                  │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 3.1 职责边界与不变量

1. **mod 只观测 + 只注入**：绝不直接修改坐标、数值、背包、金币、场景对象；所有动作经 Godot `Input` 与游戏自身 UI 信号（如 `BuyButton.pressed`）触发，由游戏逻辑自行完成状态变更。
2. **决策只在 Python**：mod 不含策略；所有选择（走位、购买、选卡、难度）由 agent 决定。
3. **安全停住**：任何时刻连接断开 → 移动注入在 TTL（默认 250ms）内归零。
4. **重连不夺权**：重连后 agent 默认进入 `OBSERVE_ONLY`，需人工 `resume` 才再次接管控制。
5. **单客户端**：mod 与 agent 同版本发布、一起演进；协议不做多版本兼容（ADR-0009）。

---

## 4. 运行时序与状态机（agent 侧）

```
                 ┌────────┐  检测到难度页(menu=difficulty_select)   ┌────────────┐
 启动/待命 ─────▶│  IDLE  │──────────────────────────────────────▶│ MENU_READY │
                 └────────┘                                       └─────┬──────┘
                      ▲                      打印英雄/武器/难度/开关     │
                      │                                               ▼
                      │                                         ┌────────────┐
                      │       非法输入重问 / 空输入重打印        │ AWAIT_INPUT│
                      │                                         └─────┬──────┘
                      │                                                │ 用户输入 Dk（已解锁）
                      │                                                ▼
                      │                                         ┌────────────┐  set_difficulty → start_run
                      │                                         │  STARTING  │──────┐
                      │                                         └────────────┘      │ 等到 wave.index==1 且 phase==combat
                      │                                                             ▼
                      │      ┌──────────────────────────── RUNNING ───────────────────────────┐
                      │      │  COMBAT（60Hz 快照驱动，30–60Hz 移动决策）                       │
                      │      │  LEVEL_UP（menu=level_up：选卡）  SHOP（shop 消息：买卖/刷新/离开）│
                      │      └───────────────┬───────────────────────────────┬─────────────────┘
                      │                      │ 胜利（第 20 波 boss 结束）     │ 死亡（player.alive=false）
                      │                      ▼                               ▼
                      │               ┌──────────────────────────────────────────┐
                      └───────────────│ ENDED：打印战报（波次/时长/金币/构建/回放）│
                                      └──────────────────────────────────────────┘
```

### 4.1 启动顺序

- 游戏与 agent **任意顺序**启动：mod 以 2s→10s 退避重连；agent 随时监听 `37650`。
- agent 收到 `hello` 后校验 `protocol_version` 与 `game_version`（锁定 1.1.15.4，ADR-0005）；不匹配 → 回 `error` 拒绝会话并提示升级/降级，mod 停止重连。

### 4.2 难度交互（核心需求）

- 检测：`menu` 消息 `phase=difficulty_select`（P1 新增，见 protocol.md §7）。
- 打印（终端）：英雄 ID（中文名）、初始武器列表、**当前英雄可选的难度范围**（读 UI 实际解锁上限，不硬编码）、模式开关（endless / ban / zone 随机 / 多人等）、当前滑杆值。
- 输入：接受 `D0`–`Dn`（n 为运行时解锁上限）；非法或超范围输入重新询问；空输入重打印清单；`q` 取消并保持待命。
- 执行：`menu_set_difficulty {value}` → 读回校验 → `menu_start_run {}` → 等待进入对局。

### 4.3 对局中

- **战斗**：60Hz 快照；反射层每帧（或隔帧）计算 `move` 向量并发送；每次发送带 `ref`，TTL 250ms 由 mod 兜底。
- **升级选卡**：`menu` 消息 `phase=level_up` 出现时暂停战斗决策，由经济层评分并发送 `menu_pick_upgrade {index}`。
- **商店**：`shop` 消息（变化推送 + 1s 心跳）驱动经济层决策：购买 / 出售 / 刷新 / 锁定 / 离开；动作串行、幂等（protocol.md §5）。
- 商店关闭判定：超过 1.5s 未收到 `shop` 消息且快照显示进入下一波。

### 4.4 断线与中断

- 断线（agent 崩溃/网络错误/用户 Ctrl+C）：mod 在 TTL 内停住移动，人物呆立，**用户可直接用鼠标键盘接管**（不抢输入）；agent 显示断连原因。
- 重连：agent 进入 `OBSERVE_ONLY`，在终端提示 `resume` 恢复接管。
- `stop`：主动断开、让出控制。可选 `pause`：向游戏注入暂停（P1 可选增强，未决见 §13）。

### 4.5 结束（一局制，ADR-0003）

- 胜利与死亡都**停止自动控制**并打印战报，不自动重试、不连刷。
- 战报内容：结果（victory/defeat）、到达波次、对局时长、金币/材料收入、最终武器与道具清单、关键属性、回放文件路径（用于复盘）。

---

## 5. IPC 协议（摘要）

完整规约见 [protocol.md](./protocol.md)。要点：

- 传输：TCP `127.0.0.1:37650`，UTF-8 NDJSON，**mod 为客户端主动连出，agent 为服务端监听**。
- 信封：`{v, seq, ts, type, ref, payload}`。
- mod→agent：`hello / snapshot / shop / event / ack / pong`；agent→mod：`welcome / ping / action`。
- v1 已实机验证；v2 计划新增 `menu` 消息（难度页/升级页/终局）与 `menu_*` 动作；单客户端直接演进，不做双版本兼容（ADR-0009）。

---

## 6. mod 设计与新增能力

### 6.1 现有模块（基线，0.2.0 实机验证）

| 模块 | 职责 | 关键实现 |
| --- | --- | --- |
| `ipc_client.gd` | 连接/握手/心跳/重连/兜底 | 退避 2s→10s；`welcome` 超时 10s；会话被拒（`error`）停止重连；`is_fallback()` 判定 2s 无消息 → 安全停住 |
| `observation.gd` | 战斗观测 | 60Hz；视野裁剪 900px；敌人上限 300、弹幕上限 400（超限 `truncated=true`）；速度按物理帧差分 |
| `movement.gd` | move 注入 | 8 向数字 + PWM（周期 100ms，占空比≈模长）；合成手柄模拟量在本引擎不可用，`prefer_analog` 仅作其他引擎预留 |
| `shop_observation.gd` | 商店观测 | 变化推送 + 1s 心跳；槽位/背包/刷新/离开/金币 |
| `shop_actions.gd` | 商店动作 | 走游戏 UI 信号；幂等账本（256 条）；串行队列（16）；购买等游戏信号、卖出/刷新差分验证；超时回执 |
| `overlay.gd` | 调试叠加层 | 显示最近观测/动作，agent 经 `debug_overlay` 动作开关 |

### 6.2 新增（P1 票据 04–06）

- **menu 观测**（`menu_observation.gd`）：
  - `phase=difficulty_select`：英雄 ID、初始武器（ID/tier）、难度 `{selected, max_selectable, unlocked}`、模式开关、`can_start`；
  - `phase=level_up`：升级卡列表（槽位/类型/ID/等级，结构以票据 04 实测为准）；
  - `phase=run_end`：结果（victory/defeat）、波次与统计入口；
  - 推送策略与 `shop` 一致：内容变化推送 + 1s 心跳；不在菜单时**不推送**（agent 以静默判定离开）。
- **menu 动作**（`menu_actions.gd`）：`menu_set_difficulty {value}`（按差值步进加减按钮或等价的滑杆设置，读回校验）、`menu_start_run {}`、`menu_pick_upgrade {index}`；幂等/TTL/ack 语义与 `shop_*` 一致；未实现动作一律拒绝（`unsupported_kind`）。
- **协议 v2 集成**：`PROTOCOL_VERSION=2`，hello/welcome 版本校验，不匹配拒绝会话。

### 6.3 引擎兼容与脆弱性

- 动态访问游戏内部：统一 `get()/call()/has_method()`，不依赖 `class_name`（ADR-0006）；数值收敛用 `engine_compat.int_arg`。
- 依赖的游戏私有字段清单见 [protocol.md](./protocol.md) §8；游戏更新后按冒烟清单（§10）逐项验证。
- 动作注入一律走游戏正常 UI 信号（ADR-0007）。

---

## 7. 决策引擎（摘要）

完整设计见 [strategy.md](./strategy.md)。三层结构（ADR-0002）：

| 层 | 频率 | 职责 |
| --- | --- | --- |
| 反射层 | 30–60Hz | 危险场构建（敌/弹幕/地雷外推）、走位方向评估、边界与风筝、平滑 |
| 战术层 | 3–5Hz | 目标选择（材料/Boss/危险区）、采集与血量管理、波次节奏 |
| 经济层 | 事件驱动 | 商店评分（Tier 表 + 经济规则）、购买/刷新/锁定/出售、升级选卡 |

- 知识库：由 mod 导出脚本从游戏资源提取物品/武器/升级/英雄静态数据生成 JSON（与锁定版本一致），叠加人工 Tier 标注；见票据 11。
- 参数外置（JSON/YAML），支持回放离线调参。

---

## 8. 安全与恢复

- **不碰游戏状态与存档**：不直接写数值、不改存档文件；系统行为等价于一名玩家正常操作。
- **停住优先**：TTL 兜底 + 断连让出（§4.4）。
- **部署安全**：部署脚本备份既有 zip 与配置，可一键重建；workshop 目录可能被 Steam 覆盖（§13 R1）。
- **回放留档**：每局回放落盘，死亡/异常时供复盘（§10）。

## 9. 版本兼容（ADR-0005）

- 锁定游戏 `1.1.15.4`；`hello.game_version` 不匹配 → 拒绝会话并在终端给出明确提示。
- Steam 建议设为"仅启动时更新"；游戏升级后先跑冒烟清单再放开。
- mod 自身版本与协议版本随仓库发布（`MOD_VERSION` / `PROTOCOL_VERSION`）。

## 10. 测试与验证（ADR-0008）

1. **冒烟测试清单**（每次部署/游戏更新后跑）：
   - 连接与握手（含版本校验）
   - 战斗观测字段 sanity（玩家/敌人数/弹幕/金币/波次）
   - move 注入（实机移动、TTL 停住）
   - 商店动作（进入商店后买/卖/刷新/锁定/离开各一）
   - 菜单观测（难度页字段）+ 难度设置/开始 + 升级选卡 + 终局识别
2. **回放**：agent 录制 NDJSON（header + 观测流 + 动作 + ack + 决策理由）；`tools/replay` 离线重放供回归与调参（票据 03）。
3. **单元测试**：协议编解码、输入解析（D 值校验）、决策评分函数、危险场几何。
4. **真实验收**：§1.2 的 10 局 ≥9 胜；每局回放 + 复盘记录。

## 11. 仓库结构与工程

```
auto-brotato/
├── mod/                     # GDScript 源码（基线：从 0.2.0 zip 提取，票据 01）
│   ├── manifest.json
│   ├── mod_main.gd
│   └── src/
├── agent/                   # Python 决策端（conda brotato）
│   ├── ab_agent/
│   │   ├── ipc_server.py    # TCP 服务端 + NDJSON + 握手
│   │   ├── state.py         # 视图与状态机
│   │   ├── decision/        # reflex / tactical / economy
│   │   ├── cli.py
│   │   └── recorder.py
│   ├── tests/
│   └── requirements.txt
├── tools/
│   ├── deploy_mod.ps1       # 打包 zip → 备份 → 写 workshop 目录 → 更新 deploy.json
│   └── replay/              # 离线回放与回归
├── recordings/              # 回放文件（不入库）
├── docs/
│   ├── architecture.md      # 本文件
│   ├── protocol.md
│   ├── strategy.md
│   ├── knowledge/           # 静态数据 JSON（票据 11 生成）
│   └── adr/
└── .scratch/auto-brotato/   # PRD 与任务票单
```

**构建与部署流程**（沿用既有部署痕迹）：

1. `mod/` 源码打包为 `BrotatoPlayer-AutoBrotato-<version>.zip`（结构 `mods-unpacked/BrotatoPlayer-AutoBrotato/...`）；
2. 备份既有 zip 与 `%APPDATA%\Brotato\` 相关配置；
3. 写入 `E:\SteamLibrary\steamapps\workshop\content\1942280\3814007307\`；
4. 更新 `%APPDATA%\Brotato\auto_brotato_deploy.json`；
5. 重启游戏由 ModLoader 加载。

**开发环境**：`conda activate brotato`；依赖最小化并固定于 `agent/requirements.txt`；开发期联网走代理 `127.0.0.1:7890`。

## 12. 路线图

| 阶段 | 内容 | 票据 |
| --- | --- | --- |
| P0 基线重建 | mod 源码入仓 + 部署工具 + 冒烟；agent 骨架；回放骨架 | 01–03 |
| P1 流程闭环 | UI 结构 spike；菜单观测/动作；升级选卡；状态机与难度交互；端到端冒烟 | 04–08 |
| P2 战斗决策 | 反射层走位 v1；战术层 v1 | 09–10 |
| P3 经济与构筑 | 知识库导出；商店/升级策略 v1 | 11–12 |
| P4 验收调优 | Ranger D0 验收（10 局≥9 胜）+ 复盘调参 | 13 |
| 延伸 | 更多英雄适配、D1+ 覆盖 | 14 |

## 13. 风险与未决清单

| # | 风险 | 影响 | 缓解 |
| --- | --- | --- | --- |
| R1 | Steam 覆盖 workshop 目录内容 | mod 被还原，系统失效 | 部署脚本可一键重建；保留 zip 备份；Steam 仅启动时更新（ADR-0005） |
| R2 | 游戏内部字段反射随更新破裂 | 观测/动作为空 | 锁版本 + 握手校验 + 冒烟清单；依赖清单见 protocol.md §8 |
| R3 | 难度页/升级页/终局页 UI 结构未实测 | P1 无法实现 | 票据 04 spike 先行 |
| R4 | 高波次敌人/弹幕超观测上限（300/400） | 决策盲区 | 监控 `truncated` 标志；必要时调参或提高上限 |
| R5 | 游戏最小化/失焦时帧率与 `_process` 行为未验证 | 后台运行时决策时效下降 | P0 冒烟加一项实测；必要时要求保持窗口可见 |
| R6 | 8 向 + PWM 的移动精度上限 | 走位不够细腻 | 参数可调（PWM 周期/阈值）；实测评估；反射层用平滑补偿 |
| R7 | 知识库（Tier/评分）质量不足 | 商店/升级决策平庸 | 人工标注 + 回放复盘迭代；评分参数可离线调 |
| R8 | 单局 20–30 分钟，真实迭代慢 | 调参周期长 | 回放离线回归为主（ADR-0008） |
| R9 | 多档案误操作 | 用错存档 | 接入前人工确认当前档案（档案 ID 读取不在本阶段范围，如需要另开票据） |
| R10 | `endless/ban` 等模式开关被打开导致流程超预期 | 非预期对局 | 难度页读取并展示；开始前终端二次确认（票据 07） |

未决事项：

- 是否需要 `menu_pause`（断开前主动暂停游戏）——P1 可选，视实机体验决定（票据 06 备注）。
- 观察上限、PWM 周期等运行参数的具体值——P2 实测后固化。

## 14. 术语表

| 术语 | 含义 |
| --- | --- |
| 波次（wave） | 对局中的一轮战斗；普通对局共 20 波，第 20 波为 Boss |
| 难度（D0–D5） | Danger 等级，提高敌人伤害/血量/速度；按英雄/存档独立解锁 |
| 快照（snapshot） | mod 以 60Hz 上送的完整战斗观测 |
| 动作（action） | agent 下发的控制指令（`move`、`shop_*`、`menu_*`） |
| TTL | 动作有效期；超时未收到新动作则 mod 归零（安全停住） |
| 反射层 / 战术层 / 经济层 | 决策引擎的三层（见 §7） |
| Tier | 物品/武器的人工强度评级，用于商店与升级评分 |
| 档案（profile） | 游戏的存档档位；当前为 profile id 1 |
| 回放（recording） | agent 落盘的 NDJSON 对局记录（观测+动作+决策理由） |
