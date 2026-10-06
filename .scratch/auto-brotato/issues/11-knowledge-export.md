# 票据 11：知识库导出（物品/武器/升级/英雄 + 人工 Tier）

- Status: ready-for-human
- Blocked by: 01
- 关联：[strategy.md §7](../../../docs/strategy.md)

## 背景

商店/升级评分需要与游戏版本一致的静态数据（id、价格、属性、类别、合并链等），不能靠手抄。

## 目标

1. mod 侧一次性导出脚本（调试模式触发或独立入口）：
   - 遍历游戏内物品/武器/升级/英雄资源，输出 JSON 到 `user://` 或经 IPC 传出；
   - 字段以 `docs/knowledge/*.json` 的表结构落盘到本仓库（含 `data_version` 与游戏版本）。
2. `docs/knowledge/tier_list.json` 骨架：人工标注字段（Tier 评级、备注、适用英雄标签），先填充 Ranger 相关常用项，其余留空。
3. 校验：导出数据与游戏内一致（抽样对照：若干武器价格/属性）。
4. 决策引擎加载接口约定：知识库版本哈希 + 与游戏版本匹配校验（不匹配告警）。

## 验收

- [x] `docs/knowledge/` 下四类 JSON 生成且抽样校验通过——实机导出 items 209 / weapons 205 / upgrades 64 / characters 50；`tools/verify_knowledge.py --sample 15` 396 项检查全部通过（覆盖性 209/209、205/205、64/64、50/50；见 Comments）
- [x] tier_list 骨架可被决策层读取（结构示例 + 若干真实条目）——`tier_list.json` 38 条 Ranger 向真实条目；`load_knowledge().tier_rating()` 精确 id/武器族回退可用，单测覆盖（见 Comments）
- [x] 导出流程可重复执行（游戏同版本结果稳定）——同一环境连续两次导出逐字节一致（`export_knowledge.py` 默认 `--repeat 2` 校验），`data_version` 由加载器按同一规范重算校验（见 Comments）

## 备注

- 具体资源路径（`items/*`、`items/upgrades/*` 等）在实现时定位；可用 ModLoader 的文件系统或运行时资源遍历。
- 不引入社区数据作为事实来源；社区共识仅用于 tier_list 人工标注。

## Comments

- 2026-10-06：票据 11 实现完成；分支 `ticket/11-knowledge-export`（worktree `C:\Users\33755\Desktop\auto-brotato-wt-11`，基点 main=`a7a6778`）保留供独立验收，未合并、未推送。提交：`d277275`（实现）+ `21e520b`（评审修复）。
- 前置核对：票据 01 已完成（Status ready-for-human、验收全勾），mod 0.2.0（协议 v2）基线在 main；实现前全量测试基线 246 passed。
- 实现：
  - mod：`mod/src/knowledge_export.gd`（触发入口/落盘/版本哈希）+ `mod/src/knowledge_entries.gd`（条目构建）；`mod/mod_main.gd` 新增调试动作 `debug_export_knowledge`。
    - 数据源：`/root/ItemService`（items/weapons/upgrades/characters/sets）与 `/root/ChallengeService.challenges`（英雄解锁按 `name` 关联）；输出 `user://auto_brotato_knowledge/{items,weapons,upgrades,characters}.json`。
    - 字段口径：`stat_deltas` 与游戏 `Effect.apply()` 一致（SUM 存储按 `value` 累加，负值即负面；`effect_sign` 仅文案）；`effects` 保留原始字段供规则表；武器 `type` 0=melee / 1=ranged，`chain` 为合并链，`sets` 为套装族；英雄含基础属性/初始武器池/解锁（默认或挑战）。
    - 稳定性：条目按 id 排序、`JSON.print(payload, "  ", true)` 字典键排序、无时间戳 → 同版本+同语言重复导出逐字节一致；`data_version` = 规范 JSON（不含 `data_version`/`mod_version`）的 SHA-256。
  - tools：`tools/export_knowledge.py`（一次性服务端连接 mod，触发导出，默认 `--repeat 2` 校验逐字节一致，复制入仓）、`tools/verify_knowledge.py`（以 PCK 静态资源为独立基准做覆盖性 + 抽样字段对照，含武器价格/属性、`apply()` 口径）、`tools/ndjson_link.py`（与 smoke_mod 共用的最小 NDJSON 链路，消除重复实现）、`tools/tests/test_verify_knowledge.py`。
  - agent：`agent/ab_agent/knowledge.py`（`load_knowledge()`/`Dataset`/`KnowledgeBase`/`tier_rating()`；启动时不匹配仅告警）、`cli.py` 启动加载 + `--knowledge-dir`（默认 `docs/knowledge`，加载失败不阻断启动）、`agent/tests/test_knowledge.py`。
  - 数据：`docs/knowledge/{items,weapons,upgrades,characters}.json`（实机导出产物入仓）+ `tier_list.json`（人工标注初版：38 条 Ranger 向条目，S/A/B/C/D + note + hero_tags；来源=社区共识整理，未经回放复盘，票据 12/13 迭代）。
  - 文档：`docs/protocol.md`（动作 + 错误码）、`docs/strategy.md` §7（schema/字段口径/稳定性边界/加载校验/更新流程）、`docs/architecture.md`（mod 文件清单、agent 树、tools 树）。
- 接口变化：
  - 协议 v2 扩展调试动作 `debug_export_knowledge`（无入参；ack 含 `dir`/`files{name,sha256,bytes}`/`data_versions`/`counts`；错误码 `item_service_missing`/`mkdir_failed:*`/`write_failed:*`/`export_failed`）；不碰游戏状态、与 TTL 无关。
  - Python：`load_knowledge(directory=None, expected_game_version=GAME_VERSION) -> KnowledgeBase`；`KnowledgeBase.tier_rating(entry)`（精确 id 优先，武器族 `weapon_id` 回退）；CLI `--knowledge-dir`。
- 验收证据：
  - 实机导出（Brotato 1.1.15.4、mod 0.2.0、zh）：items 209 / weapons 205 / upgrades 64 / characters 50 / sets 15；连续两次导出 ack 哈希与文件哈希完全一致（逐字节稳定）；modloader log 留有导出成功记录。
  - PCK 独立对照：`python tools/verify_knowledge.py --sample 15` → **396 项检查全部通过**（覆盖性 209/209、205/205、64/64、50/50；抽样价格/属性/类别与 `Effect.apply()` 口径一致）。
  - 全量测试：`python -m pytest agent/tests tools/replay/tests tools/tests -q` → **267 passed**（实现 268 → 评审后 267：移除越界英雄名接线的 2 测 + 移除 `character_names` 1 测，新增哈希校验/ tier 版本校验 2 测）。
  - 版本校验路径单测：仓库数据加载零告警；篡改条目触发 `data_version 校验失败` 告警；`game_version` 不匹配、`tier_list` 版本不一致、非法评级（丢弃）均有测试。
- 代码评审（Standards/Spec 双轴，子代理并行）与修补（`21e520b`）：
  - 复用优先：抽取 `tools/ndjson_link.py`（`Link`/`wait_for_hello`/`PROTOCOL_VERSION`），`smoke_mod.py` 与 `export_knowledge.py` 同源引用（此前 export 工具复制粘贴了整段链路）。
  - Spec 缺口补齐：`data_version` 由加载器按 mod 侧同一规范重算比对（此前仅透传，无法检测文件被改动/非本工具导出）；`tier_list.json` 的 `game_version` 与数据集一致性检查。
  - 数据洁净：tier 非法评级由「保留 + 告警」改为「丢弃 + 告警」（避免脏数据流入决策层）。
  - 范围回收：移除 `cli.py`/`session.py`/`menu_view.py` 的英雄名展示接线与 `KnowledgeBase.character_names()`（Spec 轴判定为工单外功能；终端切换到 characters.json 待后续票据，`hero_names.py` 继续作缺省来源并在 docstring 记录）。
  - 资源释放：`export_knowledge.py` 在 `finally` 关闭 mod 连接（含提前返回路径）。
  - 可读性：`knowledge_entries.gd` 补枚举注释（`WeaponData.Type` 0/1、`StorageMethod` 0=SUM、合并链上限 16 为防御性保护）。
  - Spec 轴未采纳项及理由：`weapons.json` 顶层 `sets`——strategy §5.1 套装联动评分（票据 12 需用）要求该数据，且每次实机导出成本高，随武器集一并导出；hero 展示接线按上条已回收。Standards 轴未采纳：`verify_knowledge.py` 一次 `read_bytes()` 载入 PCK（一次性本地校验工具，~148MB 内存可接受）；tools 内数据集名重复定义（各工具保持独立、校验器刻意不依赖被校验的 agent 常量）。
- 已知限制：
  1. 导出稳定性边界 = 同游戏版本 + 同 UI 语言（`name` 为 `TranslationServer` 译名，切换语言会改变 `data_version`）；锁定环境 zh（architecture §2.1），策略/文档已按此记录。
  2. `data_version` 重算依赖 Godot `JSON.print` 与 Python `json.dumps` 的浮点/转义一致；当前全量数据验证一致，未来游戏数据出现边缘浮点值时可能误报（仅告警不阻断）。
  3. tier_list 为社区共识初版（38 条），未经实机回放复盘；票据 12/13 迭代，更新 `updated_at` 并保持 `game_version` 与数据集一致。
  4. `tools/verify_knowledge.py` 为开发机一次性校验（载入 PCK 全量字节），非逐块流式；不用于运行时/CI 常规路径。
  5. 英雄名展示仍走 `hero_names.py` 临时表；知识库 `characters.json` 已含全部 50 名官方译名，接线待后续票据。
