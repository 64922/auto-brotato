# 票据 09：反射层——危险场与走位 v1

- Status: ready-for-human
- Blocked by: 03, 08
- 关联：[strategy.md §3](../../../docs/strategy.md)、票据 10

## 背景

"高水平"的第一块基石：60Hz 观测下的稳定走位/避弹/风筝。瞄准由游戏 auto-aim 负责，本层只出 `move` 向量。

## 目标

1. `agent/ab_agent/decision/reflex.py`：
   - 危险场：敌人（速度外推 + 半径）、敌方弹幕（`vel` 外推 + `ttl`）、地雷、场地边界；时间窗多采样（0.15–0.8s）；
   - 候选方向评分（16–24 方向）：危险代价 + 边界 + 战术期望位置 + 抖动平滑；输出向量 30–60Hz 持续发送；
   - 风筝距离带：按武器射程（由知识库或默认值）维护理想距离；对近战敌保持距离、对弹幕横向位移；
   - 血量紧急策略与 `invuln` 穿越（可配置）；
   - `truncated=true` 时提高保守权重。
2. 与战术层接口（期望位置/距离带/权重），战术层缺失时用默认目标（场地中心/最近材料）。
3. 回放回归：用票据 03 录制回放评估走位指标（危险暴露、受伤次数、贴边时长），输出对比报告。

## 验收

- [ ] 实机：第 1–8 波在无商店/升级干扰下稳定存活（多次运行死亡波次显著优于占位策略）——待人工前置后实机（本机 GUI 自动化受限，见 Comments）
- [x] 回放指标报告可复现；参数集中在配置文件
- [ ] 不产生 8 向 PWM 下的明显抖振（观感 + 回放方向直方图）——离线直方图与转角指标已达成（见 Comments），观感待实机

## 备注

- 决策频率与平滑参数（如每 2 帧重算 + 指数平滑）在实现中实测确定。
- 不追求极限闪避；先稳后精。

## Comments

- 2026-10-06：票据 09 离线部分实现完成；分支 `ticket/09-reflex-navigation`（worktree `C:\Users\33755\Desktop\auto-brotato-wt-09`，基点 main=`345ffd1`）保留供独立验收，未合并、未推送。提交：`eca49ad`（实现）+ `c3d4a33`（评审修复）。
- 前置核对：票据 03（`5dd4dcc`）、08（`345ffd1`）已在 main 且验收全勾；实现前基线测试 140 passed。
- 实现：
  - `agent/ab_agent/decision/`（新）：`config.py` + `config/reflex.json`（全部权重/阈值外置，键严格校验）、`geometry.py`（协议字段解析/几何）、`danger.py`（四类危险源提取与预筛 + 势函数 `w·(radius/gap)²`）、`reflex.py`（`ReflexController`：16 候选方向 + 上一方向评分，时间窗多采样 0.15/0.3/0.5/0.8s 按衰减叠加，指数平滑 + 模长钳制，30Hz 重算；`TacticalIntent` 战术接口；低血紧急、`invuln` 穿越、`truncated` 保守放大；无 IO/无随机，时间由调用方传入，回放可复现）。
  - `agent/ab_agent/move_control.py`（新）：`MoveController` Protocol（对局编排与回放评估共用）。
  - `run_control.py`：走位默认 `ReflexController`，可注入控制器；`cli.py`：`--reflex-config`、`--move-controller {reflex,placeholder}`、会话 tick 60Hz；`session.py`：文案与停止语义不变。
  - `tools/replay/movement_metrics.py` + `movement.py`：回放走位指标报告（危险暴露/暴露步占比/最近间距/贴边步占比/模长/方向切换率/决策转角/8 向直方图/录制受伤；`--config/--baseline/--seed/--max-seconds/--json`），纯文件可复现。
  - 测试：`agent/tests/test_reflex.py`（危险场几何/评分/风筝/边界/紧急/平滑/节流等）、`test_reflex_config.py`（键校验/数值范围/缺键报错）、`tools/replay/tests/test_movement.py`（口径/收集/可复现/JSON/错误路径）。
- 参数调优（离线回归 4 个含战斗录制后定稿）：`smoothing_alpha=0.8`、`turn_penalty=1.6`、`min_distance_ratio=0.2`；性能预筛 `consider_radius=550`、`max_threats_per_kind=80`（极载 300 敌 + 400 弹幕：24ms → 7.2ms/决策，30Hz 有余量）。
- 验收证据（离线）：
  - 全部自动化：`python -m pytest agent/tests tools/replay/tests tools/tests -q` → 188 passed（实现前 140）。
  - 回放对比（默认参数；指标为 暴露均值 / 贴边步占比 / 决策转角）：

    | 录制（时长，波次） | 反射层 | 占位基线 |
    | --- | --- | --- |
    | 141040（127.5s，波1–2） | 0.0411 / 0% / 5.38° | 0.0391 / 0% / 11.28° |
    | 192913（83.1s，波1–3） | 0.1130 / 0.03% / 4.52° | 0.1302 / 14.61% / 16.58° |
    | 193631（295.7s，波1） | 0.0199 / 0% / 3.56° | 0.0333 / 2.77% / 12.48° |
    | 194129（89.1s，波1–4） | 0.1686 / 0% / 4.01° | 0.2190 / 21.98% / 14.76° |

    说明：除 141040 暴露略高（+0.002）外，其余三项暴露/贴边/转角均优于占位；四份录制的转角与切换率全面显著更低（抖振离线证据）。JSON 留档 `%TEMP%\opencode\t09-reports\`；重跑逐位一致（`test_report_is_reproducible` 另保证两次构建逐字符一致）。
  - 实机：**未完成**（见已知限制①）；离线报告可替代性有限：不模拟策略改变后的战局演进，只评估「同一录制环境下各策略会选什么方向」。
- 代码评审（Standards/Spec 双轴，子代理并行）与修补（`c3d4a33`）：
  - 拆分超 500 行文件：`reflex.py` 538 → 367 行，危险场/几何独立为 `danger.py`/`geometry.py`；`movement.py` 529 → 245 行，指标评估（295 行）独立为 `movement_metrics.py`（文件行数工程约束）。
  - `0.2` 距离下限系数外置为 `danger.min_distance_ratio`；`_threat_position` 删除未用默认参；`movement.py` 去重波次/时长推导为 `_snapshot_span`。
  - Spec 轴确认：风筝带用「默认值」符合工单口径（知识库票据 11 未就绪）；`--move-controller` 注入与预筛参数为实机对照与性能验收所必需（记录在案，避免后续误判为 scope creep）。
  - 修补后全套 188 passed；四份录制复跑数值与调参时一致。
- 待人工（实机验收协议，请求配合）：
  1. 前置：启动游戏（mod 已部署）→ 选游侠 Ranger + 初始 SMG、关闭无尽/禁用 → 停在难度页；agent 侧由我运行 CLI（`python -m ab_agent.cli`，默认 reflex），难度输入 `D0`。
  2. 一局死亡后自动打印战报（记录死亡波次）；重复 ≥2 局；再用 `--move-controller placeholder` 重复 ≥2 局对照。
  3. 观感项（8 向 PWM 抖振）由人工在实机确认。
- 已知限制：
  1. 实机 1–8 波验收未完成：本机合成输入（SendKeys/SendInput/keybd_event/AttachThreadInput 强激活）对该定制 Godot 3.7 引擎均无效（已在普通 WinForms 窗口验证注入本身可用），且英雄/武器选择按设计保留人工；需人工前置后才能实机驱动。
  2. `truncated=true` 仅整体放大危险权重（1.4×），未按「最近截断距离」推定密度（快照无该字段）。
  3. 风筝距离带为默认 `[260, 420]`，未接武器射程（知识库票据 11）；对弹幕横向位移靠危险场自然绕开，无专门策略。
  4. 回放「录制受伤」为既定事实、不能归因策略；策略对比使用暴露/贴边/抖振指标，环境由录制决定（不做战局模拟）。
  5. 敌方弹幕 `ttl=0` 按「不截断」处理：mod 读 `_time_until_max_range`（observation.gd 将负值 clamp 为 0），9 份录制中敌方弹幕该字段恒为 0；若实为「已达射程」则本实现偏保守（多躲），实测该录制下未出现异常停滞。
  6. `danger_at` 当前仅测试与票据 10 预留（战术层目标位评估），暂无生产调用方；保留以承接战术层接口。
- 接口变化：新增 Python 内部接口（`ReflexController`/`TacticalIntent`/`MoveController`）与 CLI 选项，无 mod/协议变更（protocol v2 不变）。
