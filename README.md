# AutoBrotato

> 全自动 Brotato 对局系统：你负责选英雄，它负责通关。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)
![Brotato 1.1.15.4](https://img.shields.io/badge/Brotato-1.1.15.4-green.svg)
![Protocol v2](https://img.shields.io/badge/IPC%20protocol-v2-orange.svg)

AutoBrotato 让 [Brotato](https://store.steampowered.com/app/1942280/Brotato/)（Steam 单机肉鸽）在无需人工操作的情况下打完整局：你在游戏中选好英雄与初始武器、停在难度选择页，系统读取当前选择并询问难度，然后自动完成全部 20 波战斗、商店购物与升级选卡，结束后输出战报与回放。

系统由两部分组成：

- **mod/**（GDScript，Godot ModLoader）：只负责战斗/菜单/商店的 60Hz 观测与动作注入，不含任何策略；
- **agent/**（Python 3.12）：IPC 服务端、会话状态机与三层决策引擎（反射层 / 战术层 / 经济层），所有决策都在这里。

## 特性

- **端到端全自动**：难度页检测 → 难度输入 → 自动开局 → 20 波战斗（走位/避弹/风筝）→ 商店买卖/刷新/锁定 → 升级选卡 → 胜败战报。
- **只走正常输入**：所有动作经游戏 `Input` 与 UI 信号触发，绝不直接修改坐标、背包、金币或存档，行为等价于一名正常玩家。
- **安全停住**：动作带 250ms TTL，链路断开即归零；`stop` 主动让出控制，重连默认 `OBSERVE_ONLY`，需 `resume` 才重新接管，随时可人工用鼠标键盘接手。
- **可复盘**：每局落盘 NDJSON 回放（观测 + 动作 + 决策理由），`tools/replay` 支持离线摘要与倍速重放，用于回归与调参。
- **可观测**：终端周期状态 + 可选游戏内调试叠加层；战报含波次、时长、经济、构建与回放路径。
- **锁版本 + 握手校验**：锁定游戏 `1.1.15.4`，协议版本或游戏版本不匹配时拒绝会话，避免游戏更新后的静默错误。

## 工作原理

```
┌──────────────────────── Brotato.exe（单机）────────────────────────┐
│ Godot ModLoader + AutoBrotato mod                                  │
│  观测：战斗快照 60Hz / 商店 / 菜单（难度页·升级页·终局）             │
│  动作：move（8 向 + PWM）/ shop_* / menu_*（均经游戏 UI 信号）      │
└──────────────────────────────┬─────────────────────────────────────┘
                     TCP 127.0.0.1:37650 · NDJSON · 协议 v2
┌──────────────────────────────┴─────────────────────────────────────┐
│ Python agent                                                       │
│  ipc_server → state → session（难度交互/开局/终局状态机）            │
│  decision：反射层 30–60Hz 走位 · 战术层 3–5Hz 目标 · 经济层 商店/升级 │
│  cli（终端交互）· recorder（回放落盘）                               │
└────────────────────────────────────────────────────────────────────┘
```

设计要点：瞄准与开火由游戏自带 auto-aim 完成（`manual_aim=false`），agent 在战斗中只需控制移动——反射层构建危险场（敌人/弹幕外推、地雷、边界）并在候选方向上评分，输出平滑的 `move` 向量；战术层负责目标（材料/Boss/安全位）与波次节奏；经济层在商店与升级事件上按知识库 Tier 与评分规则决策。

架构细节见 [docs/architecture.md](docs/architecture.md)，IPC 协议见 [docs/protocol.md](docs/protocol.md)，决策设计见 [docs/strategy.md](docs/strategy.md)。

## 仓库结构

```
auto-brotato/
├── mod/                    # GDScript mod（观测 + 动作注入，无策略）
│   ├── manifest.json
│   ├── mod_main.gd
│   └── src/
├── agent/                  # Python 决策端（conda 环境 brotato）
│   ├── ab_agent/           # ipc_server / state / session / cli / recorder
│   │   └── decision/       # 反射层 / 战术层 / 经济层（参数外置 config/*.json）
│   ├── tests/
│   └── requirements.txt
├── tools/
│   ├── deploy_mod.ps1      # 打包 + 备份 + 部署到 workshop 目录
│   ├── smoke_mod.py        # mod 冒烟（握手/快照/move/TTL）
│   ├── smoke_e2e.py        # 端到端闭环冒烟（难度页 → 第 2 波 → 回放校验）
│   ├── export_knowledge.py # 从游戏资源导出静态数据到 docs/knowledge/
│   ├── verify_knowledge.py # 知识库与游戏 PCK 抽样对照
│   └── replay/             # 离线回放与走位/战术/经济对照报告
├── docs/                   # 架构 / 协议 / 策略文档、ADR、知识库
├── recordings/             # 回放落盘（不入库）
└── .scratch/auto-brotato/  # PRD 与任务票单
```

## 环境要求

| 项 | 要求 |
| --- | --- |
| 操作系统 | Windows（当前开发与验收环境） |
| 游戏 | Brotato `1.1.15.4`（Steam app `1942280`）+ Godot ModLoader `6.1.0` |
| Python | 3.12（conda 环境 `brotato`；运行时仅标准库，测试需 pytest） |

## 快速开始

### 1. 部署 mod

```powershell
# 打包 mod/ → 备份既有 zip 与配置 → 写入 Brotato workshop 目录
powershell -ExecutionPolicy Bypass -File tools\deploy_mod.ps1
```

脚本默认指向 `E:\SteamLibrary\steamapps\workshop\content\1942280\3814007307`，可通过 `-WorkshopDir` 等参数覆盖；部署后重启游戏由 ModLoader 加载。

### 2. 启动 agent

```powershell
conda activate brotato
cd agent
python -m ab_agent.cli
```

### 3. 游戏内操作

1. 打开 Brotato，**手动选择英雄与初始武器**，停在难度选择页；
2. 终端会打印英雄、初始武器、当前解锁的难度范围与模式开关，输入 `D0`–`Dn` 选择难度（模式开关有开启项时需 `y` 二次确认）；
3. 之后无需任何操作：自动开局、打满 20 波（含商店与升级选卡），结束打印战报与回放路径。

终端命令：`status`（状态）/ `stop`（断开让出控制）/ `resume`（恢复接管）/ `quit`（退出）/ `help`（帮助）。

## 开发与验证

```powershell
# 全量测试（仓库根目录）
python -m pytest agent/tests tools/replay/tests tools/tests -q

# mod 冒烟：握手 / 快照频率 / move 注入 / TTL 停住
python tools\smoke_mod.py --seconds 12

# 端到端闭环冒烟：停在难度页后运行，自动输入难度并打到第 2 波
python -m tools.smoke_e2e

# 知识库导出与校验
python tools\export_knowledge.py
python tools\verify_knowledge.py

# 离线回放：摘要 / 10 倍速重放 / 从第 120 秒开始
python -m tools.replay recordings\<file>.ndjson --summary
python -m tools.replay recordings\<file>.ndjson --speed 10 --seek 120
```

## 安全与合规

- 不修改游戏文件与存档，不联机，不影响他人；
- 观测与动作全部经游戏正常流程，游戏 `pause_on_focus_lost=false` 时可后台运行；
- 任何断连都会在 TTL 内安全停住，控制权随时可交还给玩家。

## 开发状态

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| P0 | mod 源码入仓、部署工具、agent 骨架、回放骨架 | 完成 |
| P1 | 菜单观测/动作、升级选卡、会话状态机、端到端冒烟 | 完成 |
| P2 | 反射层走位、战术层 | 完成 |
| P3 | 知识库导出、经济层（商店/升级） | 完成 |
| P4 | Ranger D0 验收（连续 10 局 ≥9 胜）与调优 | 进行中 |
| 延伸 | 更多英雄适配、D1+、发现道具页、菜单动作稳定性 | 计划中 |

验收基准：英雄 `character_ranger`（游侠），D0 连续 10 局自动对局 ≥9 局通关，除输入难度外全程无人工干预，不使用暂停、作弊或直接改游戏状态。

## 文档导航

- [docs/architecture.md](docs/architecture.md) — 系统架构（主文档）
- [docs/protocol.md](docs/protocol.md) — mod ⇄ agent IPC 协议规约
- [docs/strategy.md](docs/strategy.md) — 决策引擎与策略设计
- [docs/adr/](docs/adr/) — 架构决策记录（ADR-0001 ~ 0009）
- [.scratch/auto-brotato/PRD.md](.scratch/auto-brotato/PRD.md) — 需求与验收标准

## 许可证

[MIT](LICENSE)
