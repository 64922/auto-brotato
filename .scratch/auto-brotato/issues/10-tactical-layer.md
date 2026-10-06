# 票据 10：战术层——目标选择、采集与血量管理

- Status: ready-for-human
- Blocked by: 09
- 关联：[strategy.md §4](../../../docs/strategy.md)

## 背景

反射层只解决"不被碰到"；得分需要材料采集、目标选择与波次节奏。

## 目标

1. `decision/tactical.py`（3–5Hz）：
   - 目标选择：材料簇（贪心 + 距离衰减 + 危险惩罚）、回血消耗品（低血优先）、Boss 应对（射程带 + 开阔方向）；
   - 波次节奏：早期偏采集、中后期偏生存、末段清场拾取；
   - 血量管理：低血阈值触发保守模式（抬危险权重、放弃远端材料）；
   - 输出：期望位置（可选）+ 目标距离带 + 权重向量给反射层。
2. 回放回归：材料收入/波次曲线、无效移动占比、死亡归因（最近 3 秒受伤来源）。

## 验收

- [ ] 回放对比：接入战术层后材料收入与存活波次同时提升（相对仅反射层）——离线代理证据已备（采集接近率↑ / 无效移动↓），实机两局对照待人工前置（见 Comments）
- [x] 低血保守模式在回放中可观察到触发与退出——192913（波 3）、194129（波 4）回放区间可复现（见 Comments）
- [x] 所有阈值/权重可配置——`config/tactical.json` + 严格键校验（见 Comments）

## 备注

- 先规则后精调；Boss 技能模式（第 20 波）需要若干次对局观察后补规则。

## Comments

- 2026-10-06：票据 10 离线实现完成；分支 `ticket/10-tactical-layer`（worktree `C:\Users\33755\Desktop\auto-brotato-wt-10`，基点 main=`0bf69ac`）保留供独立验收，未合并、未推送。提交：`4c8e185`（实现）+ `59030eb`（评审修复）。
- 前置核对：票据 09 四个提交已在 main 且验收全勾（实机 5 局 4/5/5/6/7 波；用户判定通过）；实现前基线测试 191 passed；09 预留给战术层的 `ReflexController.danger_at` 已接入生产调用。
- 实现：
  - `agent/ab_agent/decision/tactical.py`（新）：`TacticalController`（4Hz，`update.interval_s`）——缓存的 `TacticalIntent` 交给反射层 30–60Hz 执行，战术层只改「目标与权重」；
    - 目标：材料簇（单链贪心聚合 + 距离衰减 `decay/(decay+d)` + `danger_at` 危险惩罚，分支限界减少危险场调用）、回血消耗品（hp 比例低于 `consumable.hp_ratio` 时优先，评分复用簇口径）、Boss（`boss.band` 射程带 + 环上 `candidates` 个候选点按「危险低 + 离边界远」选开阔方向）；
    - 节奏：早期（≤8 波，低危险权重/高吸引）→ 中后期（更高危险权重）→ 清场窗口（敌人 ≤3，吸引拉满）；Boss 优先于清场；
    - 血量：进入/退出迟滞（`enter_hp_ratio`/`exit_hp_ratio`）的保守模式——危险 × 系数、吸引压低、放弃远端材料（`max_pickup_distance`）；消耗品目标不压低吸引（回血优先）；
    - 输出：`TacticalIntent`（expected_position 可选 + distance_band（Boss）+ danger_scale/attraction_scale 权重向量）；无目标时空意图，反射层走内置默认（场地中心）。
  - `agent/ab_agent/decision/tactical_config.py` + `config/tactical.json`（新）：全部阈值/权重外置，严格键校验（多/少键报错、数值范围、迟滞与波次边界有序性、3–5Hz 节拍契约 0.2–0.34s）。
  - `agent/ab_agent/decision/config_util.py`（新）：抽取 reflex/tactical 共用的配置校验（第二个真实用例出现后抽象；`ReflexConfigError`/`TacticalConfigError` 继承 `DecisionConfigError`，错误文案与既有测试不变）。
  - 接线：`run_control.py` 默认控制器 → `TacticalController`；`cli.py` 新增 `--tactical-config`，`--move-controller {tactical,reflex,placeholder}`（默认 tactical，reflex/placeholder 为对照基线）。
  - `tools/replay/tactical_metrics.py` + `recording_facts.py` + `tactical.py`（新）：回放报告——采集接近率、无效移动占比、低血保守区间、材料/波次曲线、死亡归因（最近 3 秒各危险源最小间距 + 受伤时刻 300px 内数量）、双录制结果对比（`--compare-with`：存活波次/材料总量/曲线/死亡时刻）。
  - 测试：`agent/tests/test_tactical.py`、`test_tactical_config.py`、`tools/replay/tests/test_tactical.py`（节拍、四段节奏、簇/消耗品/Boss 目标、保守迟滞、默认接线、回放事实与可复现）。
- 接口变化：新增 Python 内部接口（`TacticalController`/`TacticalState`/`TacticalConfig`）与 CLI 选项；无 mod/协议变更（protocol v2 不变）。
- 验收证据（离线）：
  - 全部自动化：`python -m pytest agent/tests tools/replay/tests tools/tests -q` → **242 passed**（实现前 191；新增 51）。
  - 4 份含战斗录制（默认参数；指标为 采集接近率 / 无效移动占比 / 危险暴露均值）：

    | 录制（时长，波次） | 战术层 | 仅反射层 |
    | --- | --- | --- |
    | 141040（127.5s，波 1–2） | 79.7% / 32.2% / 0.061 | 41.9% / 50.5% / 0.036 |
    | 192913（83.1s，波 1–3） | 79.5% / 21.0% / 0.157 | 73.0% / 20.1% / 0.154 |
    | 193631（295.7s，波 1） | 93.5% / 15.9% / 0.038 | 72.0% / 22.1% / 0.018 |
    | 194129（89.1s，波 1–4） | 83.2% / 21.7% / 0.203 | 73.6% / 24.2% / 0.189 |

    说明：战术层采集接近率全面更高、无效移动 4 份中 3 份更低；暴露均值略升（+0.0005～+0.025），为「早期偏采集」的已知代价（先规则后精调）。
  - 低血保守模式回放可观察：192913 触发 1 次（波 3，t≈189s→192s）、194129 触发 1 次（波 4，t≈85.8s→89.1s），触发与退出均在报告中可见；单测覆盖迟滞进出。
  - 死亡归因可用：192913 5 次 / 194129 3 次 hp 下降事件，含最近 3 秒敌人/弹幕/地雷最小间距（例：194129 t=86.0s 受伤 3hp，敌人最近 5px）。
  - 双录制对比冒烟（194129 vs 192913）：存活波次 4 vs 3、材料总量 99 vs 91，报告与 JSON 正常。
  - JSON/文本留档：`%TEMP%\opencode\t10-reports\`（194129/192913/193631/141040 的 json + 194129/192913 的 txt）；重跑逐位一致，另有 `test_report_is_reproducible`/`test_main_json_is_reproducible` 保障。
  - 性能（合成极载 300 敌 + 400 弹幕 + 50 材料 + truncated）：反射单次 ≈6.6ms，战术 `_decide` ≈21ms（4Hz，已加簇评分分支限界；高危险密度下危险场调用仍接近簇数），`next_move` ≈30ms；实机/回放常规负载远低于此（4 份录制 5k–18k 快照全量三引擎对比 2–10s）。
- 代码评审（Standards/Spec 双轴，子代理并行）与修补（`59030eb`）：
  - 复用优先：`movement_metrics` 公开 `predicted_point`/`clearance_at`/`threat_position`/`sequence`/`player_point`/`snapshot_span`，`tactical_metrics` 删除重复实现并拆分出 `recording_facts.py`（文件规模 460 → 202 + 254 行）；`recording.wave_index` 统一波次解析。
  - 节拍契约收紧：`update.interval_s` 校验范围 0.2–0.34s（3–5Hz 约定），补测越界用例。
  - 其他修补：`build_engines` 返回注解改为 `MoveController`；双录制对比去掉冗余 Path 数据簇；合并 `_format_curve`/`_curve_text`；`docs/architecture.md` §11 决策模块树同步。
  - Spec 轴 scope 说明（记录在案，避免后续误判）：默认控制器切到战术层、`--baseline placeholder`、`config_util` 抽取均为「接入生产 + 与既有报告同口径对照 + 第二个配置用例」所必需；未引入工单外功能。
  - Spec 轴未采纳项及理由：死亡时刻血量/位置、金币曲线、无谓受伤/贴边时长（strategy §8.1 模板项）——本票口径为「材料收入/波次曲线 + 无效移动占比 + 死亡归因」，金币属票据 12，金币买入/卖出才会改变其含义；贴边时长已在 movement 报告 JSON（`edge_fraction`）。如需 §8.1 全模板，建议另开小票。
- 待人工（验收项 1 的实机对照协议，请求配合）：
  1. 前置：游戏（mod 已部署）→ 游侠 Ranger + 初始 SMG、关闭无尽/禁用 → 停在难度页；
  2. 实机 A（战术层，默认）：`cd agent && python -m ab_agent.cli` → 输入 D0，打 1–2 局（录制自动落盘）；
  3. 实机 B（仅反射层对照）：`python -m ab_agent.cli --move-controller reflex` → 打 1–2 局；
  4. 对比：`cd .. && python -m tools.replay.tactical recordings\<战术局>.ndjson --compare-with recordings\<反射局>.ndjson`，核对存活波次与材料总量/曲线。
- 已知限制：
  1. 实机验收未完成（需人工前置；agent 不自动选英雄/武器，见架构非目标）。
  2. 回放不模拟战局演进：单录制对比只评估「同一环境下会成为什么方向」；材料收入/存活提升的结论必须来自双实机录制（工具已备）。
  3. Boss 规则为初版（射程带 + 开阔方向）；第 20 波 Boss 技能模式需实机观察后补规则（工单备注）。
  4. 高危险密度下战术决策 ≈21ms@4Hz（合成极载，含每簇危险场评估；分支限界在低危险环境可显著减少调用）。
  5. hp 下降归因按波次内比较（与 movement `recording_damage` 同口径），跨波边界跳变不计。
  6. 消耗品评分复用材料簇距离衰减/危险权重（无独立配置）；如需独立调参再拆。
  7. 距离带仅 Boss 模式显式设置，其余沿用反射层默认带（先规则后精调）。
