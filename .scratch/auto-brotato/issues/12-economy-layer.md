# 票据 12：经济层——商店与升级选卡策略 v1

- Status: ready-for-human
- Blocked by: 03, 06, 11
- 关联：[strategy.md §5](../../../docs/strategy.md)

## 背景

商店与升级是"高水平"的另一半：评分购买、刷新预算、锁定与出售、升级选卡。

## 目标

1. `decision/economy.py`：
   - 物品评分（属性权重 × 增量 + 特殊效果 + 协同 − 价格机会成本）；
   - 购买/刷新/锁定/出售规则与预算曲线；同 id 武器合并优先；
   - 升级选卡评分（无价格项；按"最缺属性"补齐）；
   - 全部动作经 `shop_*` / `menu_pick_upgrade` 下发，处理 ack 与失败重试规则（有限、按错误码区分）；
   - 决策理由写入回放（`{kind:"decision"}` 行，票据 03 接口）。
2. 商店吞吐：动作串行（mod 侧队列），agent 侧按 ack 推进，不并发；离开时机（无高价值可买或预算耗尽）。
3. 回放回归：不同参数集在回放库上对比（购买序列评分、金币曲线、最终构建强度）。

## 验收

- [x] 实机商店：能自动买/卖/刷新/锁定并正确离开（≥10 个商店循环无卡死）——实机 14 个商店循环（第 2–15 波）全部处理并离开，无卡死；锁定因全程金币充足未出现实机机会（规则单测覆盖，见 Comments）
- [x] 升级选卡在全部出现场景下被正确选择——实机 20 次选卡；全分支（未知卡/降级/武器位满/最缺属性）单测覆盖（见 Comments）
- [x] 回放中可复现同一决策序列（固定参数 + 同一回放输入）——单测 + 实机录制重放输出逐字节一致（见 Comments）
- [x] 关键参数集中在配置文件——`config/economy.json` 启动校验；执行层冷却/发送上限为与评分无关的常量（见 Comments 已知限制 3）

## 备注

- 失败重试规则需谨慎：`insufficient_gold` 不重试，超时可重试一次（避免动作风暴）。

## Comments

- 2026-10-06：票据 12 实现完成；分支 `ticket/12-economy-layer`（worktree `C:\Users\33755\Desktop\auto-brotato-wt-12`，基点 main=`ad40d55`）保留供独立验收，未合并、未推送。提交：`3178556`（实现）+ `15ad276`（评审修复）+ `c46608f`（实机卡死修复）+ `f2cd38c`（架构文档）。
- 前置核对：票据 03/06/11 已完成且已入 main；实现前全量测试基线 267 passed。
- 实现：
  - `agent/ab_agent/decision/economy_model.py`：EconomyContext / ScoreBreakdown / Appraisal / ShopPlan / UpgradePlan 与观测辅助（`as_int`/`items`/`current_stats`）。
  - `decision/economy_scoring.py`：OfferScorer——属性权重 × 增量 + 效果规则表 + Tier 标注 + 同族合并（+`merge_bonus`）+ 套装跨档边际 + 价格机会成本（早/中/晚曲线）+ 升级短板（`need_weight`）+ 武器 DPS + 最终构建强度；未知条目用 `upgrade.unknown_score` 惩罚。
  - `decision/economy.py`：EconomyPlanner（纯函数、无随机、无 IO）——买（`score >= buy_threshold` 且扣价后高于波次安全余量）、卖（仅武器、低于 `sell_below_score`、保留 `sell_min_weapons`）、刷新（全低价值且预算 = base + 每波增量）、锁定（高价值买不起且 `lock_enabled`）、离开（无高价值/预算耗尽）；升级选卡复用同一价值函数（无价格项）+ 最缺属性短板。知识库缺失时降级：商店直接离开、升级选第一张可选卡。
  - `decision/economy_runner.py`：串行动作执行（一次一个在途动作）、ack 按错误码分类重试、成功动作后冻结同指纹旧视图（数据变化推送 + 1s 心跳内必达）、决策落盘（layer=`economy`，reason 为中文评分明细）。重试策略：`insufficient_gold` 不重试；超时类（含 `pick_timeout`/`no_ack`）重试一次后抑制；`shop_leave` 例外——无替代动作，持续冷却重试（抑制会卡死在商店）；`busy`/`cannot_leave`/`shop_closed` 冷却重试；`not_in_shop`/`not_in_level_up` 视为访问结束；任务异常/发送阻塞必定复位在途状态（`finally` + `SEND_DRAIN_TIMEOUT_S`）。
  - `decision/economy_config.py` + `decision/config/economy.json`：参数外置（阈值/权重/价格曲线/安全余量/刷新预算/锁定/英雄档案），启动时结构校验。
  - 接线：`run_control.py`（注入经济层、`begin(hero_id)`、商店/升级委托）、`session.py`（构建并透传）、`menu_view.py`（商店摘要可选动作标签）、`cli.py`（`--economy-config`；无知识库时降级不阻断启动）。未注入经济层时保留票据 07 固定行为（既有测试不变）。
  - 工具：`tools/replay/economy.py` + `tools/replay/tests/test_economy.py`——同一录制上对比不同参数集（购买序列评分/金币曲线/最终构建强度；影子背包为近似口径），确定性输出。
- 接口变化：只使用协议既有 `shop_*` / `menu_pick_upgrade` 与 `decision` 落盘接口，mod 侧零改动；agent 新增 `--economy-config` 与 `RunSession(economy=…)` 注入点。
- 测试与验证：
  - 全量测试：`python -m pytest agent/tests tools -q` → **341 passed**（经济评分/规划 43、runner 13、接线 4、配置 11、回放工具 9 等新增用例）。
  - 回放确定性：同一实机录制两次 `python -m tools.replay.economy <rec> --json` 输出逐字节一致；`--config` 严格阈值（buy_threshold=100）购买数 1→0，参数集对比有效。
  - 实机（危险1，13 分 37 秒至第 15 波战败；回放 `recordings/20261006-231800-unknown-unknown.ndjson`）：**14 个商店访问**（第 2–15 波）全部正常处理并离开；决策 76 条 = 买 17 / 卖 1 / 刷新 24 / 离开 14 / 选卡 20；经济日志 76 行、异常/放弃 0；最大决策间隔 67.4s（正常战斗时长，非卡顿）。终局构建与战报/截图一致：6 武器（revolver T1×2、laser_gun T3/T2、smg T3/T2）+ 道具 banner/gambling_token/lens。
  - 实机中发现并修复（`c46608f`）：升级选卡任务一次异常/发送阻塞会导致共享 `pending` 永不复位，商店与升级全部停摆。首个实机回放 `recordings/20261006-230849-unknown-unknown.ndjson` 复现：选卡 `decision` 已记录但动作未发出，此后 116 条商店推送全部未处理。修复：`_execute_*` try/finally 复位 + IPC `drain` 2s 超时 + 回归测试。
- 代码评审（Standards/Spec 双轴，子代理并行）与修补（`15ad276`）：补旧视图冻结（防级联误卖/重复锁定）、`shop_leave` 超时持续重试（原先实际无限重试且提示矛盾）、`upgrade.unknown_score` 生效（原为死参数）、回放道具按 `count` 合并、消除重复 `_as_int`/选项签名（`run_control` 复用 `economy_model.as_int` 与 `economy_runner.options_signature`）。
- 已知限制：
  1. 实机未触发锁定（全程金币充足，无「高价值买不起」场景）；锁定规则由单测覆盖，实机触发需高难度/低成本局复验。
  2. 起局依赖游戏 UI 焦点（票据 06/07 逻辑，非本票）：mod `menu_set_difficulty` 在目标已选中时为空操作、不重置焦点；若用户先行手动导航，随后 `menu_start_run` 按下的是「当前焦点元素」而非目标难度元素，且 mod 的成功判定（难度页离场 + `/root/Main` 就位）可能误判，表现为 `ok` 后弹回难度页。本次通过「重开游戏保持干净焦点」规避。建议后续票据改为固定按下目标难度元素。
  3. `RETRY_COOLDOWN_S` / `SEND_DRAIN_TIMEOUT_S` 为执行策略常量（与评分无关）未入配置；DPS 的 ×60 为「冷却帧→秒」换算常量。
  4. 回放影子背包的合并/道具堆叠为近似口径，最终构建强度以录制末真实背包为准。
