# 票据 02：Python agent 骨架（IPC 服务端 + 状态模型 + CLI 骨架）

- Status: ready-for-human
- Blocked by: 无（可与 01 并行）
- 关联：[protocol.md](../../../docs/protocol.md)、[architecture.md §3/§4](../../../docs/architecture.md)

## 背景

决策端为零：需要在 conda 环境 `brotato`（Python 3.12）建立 agent 进程：**监听 37650**，接受 mod 主动连接（mod 是 TCP 客户端）。

## 目标

1. `agent/ab_agent/ipc_server.py`：
   - asyncio TCP server；逐行解析 NDJSON 信封（`v/seq/ts/type/ref/payload`）；
   - 握手：收 `hello` → 校验 `protocol_version` 与 `game_version==1.1.15.4`（ADR-0005）→ 不匹配发 `error` 并断开；
   - 匹配则发 `welcome`（config：snapshot_hz=60、action_ttl_ms=250、debug_overlay=false）；
   - 消息分发（snapshot/shop/event/ack/pong）；发 `action` 并维护 `ref` 序号与 ack 匹配；
   - 心跳（可选 `ping`）；断连检测与日志。
2. `agent/ab_agent/state.py`：最新快照/商店视图 + 时间戳；链路健康（上次快照时间）；`OBSERVE_ONLY` 标志。
3. `agent/ab_agent/cli.py` 骨架：启动参数、状态打印（连接状态、链路频率、波形摘要）、命令 `status/stop/resume`（stop=断开让出；resume=恢复接管）。
4. `agent/requirements.txt`（最小依赖）与 `agent/tests/`（协议编解码、信封解析的单元测试）。

## 验收

- [x] `conda activate brotato` 后直接运行，无第三方缺失依赖
- [x] 与票据 01 部署的 mod 实机握手成功，终端显示快照频率与波次
- [x] 版本不匹配（临时篡改常量模拟）时拒绝会话并给出清晰提示
- [x] 单元测试通过（pytest 或 unittest）

## 备注

- 单客户端假设：仅接受一个活动连接；重复连接时替换旧连接并告警。
- 不做决策逻辑；不做回放（票据 03）。

## Comments

- 2026-10-06：票据 02 实现完成，保留分支 `ticket/02-agent-skeleton` 与 worktree `C:\Users\33755\Desktop\auto-brotato-t02` 供独立验收（未合并、未推送）。基线 main=27995e9；前置依赖票据 01 已核对（mod 0.2.0 入仓且部署完成，验收项全勾）。
  - 交付物：`agent/ab_agent/{__init__,protocol,state,ipc_server,cli}.py`；`agent/tests/{__init__,conftest,test_protocol,test_state,test_ipc_server}.py`；`agent/requirements.txt`；根 `.gitignore`（首次入仓 Python 包，忽略 `__pycache__/`、`*.pyc`、`.pytest_cache/`）。另同步 `docs/architecture.md` §11 目录树加入 `protocol.py`。
  - 实现说明/接口变化：
    - `protocol.py`（新增模块，纯函数）：`PROTOCOL_VERSION=1`、`GAME_VERSION=1.1.15.4`；`Envelope`、`parse_line`（严格校验 v/seq/ts/type/payload，非法行抛 `ProtocolError` 由服务端丢弃）、`encode`、`check_hello`、`welcome_payload`、`error_payload`。v1 协议未定义 `error` 载荷结构，本票约定 `{"code":"version_mismatch","message":"<中文说明>"}`（mod 仅记日志，不解析）。
    - `ipc_server.py`：`IpcServer.start/shutdown/bound_port/connected/disconnect_client/send_action/wait_ack`。NDJSON 单行上限提至 8MiB（asyncio 默认 64KiB 不足以承载 300 敌+400 弹幕的快照行）；心跳默认 1s（保证 mod 2s 兜底不误触发），心跳循环同时每 0.25s 回收未回执动作（协议 §5.2 的 2s 双保险，ping 关闭时仍生效）；`send_action` 先登记 pending 再发送（避免快速 ack 竞态），`track_ack` 可选；单客户端替换、断连在途动作按 `link_lost` 处理。
    - `state.py`：`AgentState` 维护握手信息/最新快照/商店/event/ack/pong、滑动窗口频率、链路健康（上次快照 ≤2s）、`observe_only`（每次握手重置为 True，架构 §3.1 不变量 4）；`describe()` 输出连接/模式/链路/频率/波次一行摘要。
    - `cli.py`：`python -m ab_agent.cli`（参数 `--host/--port/--ping-interval/--status-interval/--log-level`）；命令 `status/stop/resume/quit/help`；周期状态打印；stdin 用守护线程+队列读取（Ctrl+C 不等待 executor 线程；stdin 关闭时保持运行）。
    - `requirements.txt`：运行时零第三方依赖（仅标准库）；测试可用 pytest 或 `python -m unittest discover -s tests -t .`（已加 `tests/__init__.py` 使两者都可用）。
  - 验证结果：
    1. 单元/集成测试：`cd agent && python -m pytest tests -q` → **50 passed**；`python -m unittest discover -s tests -t .` → **OK（50）**。覆盖：信封编解码/畸形输入/hello 校验；状态频率/链路健康/OBSERVE_ONLY 重连语义/摘要；假 mod TCP 端到端（握手 welcome 配置、协议与游戏版本不匹配拒绝并断开、握手前消息忽略、单客户端替换、action ref 递增与 ack 匹配、失败 ack、断连在途动作 link_lost、observe_only 拦截、心跳 ping/pong、>64KiB 大行解析、乱行丢弃不断链）。
    2. CLI 端到端 mock（临时脚本未入库）：启动 → 握手 → 状态行含 `链路=健康`、快照频率、`第 2 波 · combat` → `stop` 后 mod 侧 EOF、重连后 `模式=OBSERVE_ONLY` → `resume` 后 `模式=接管` → `quit` exit 0。
    3. 实机（mod 0.2.0 已部署，Brotato 1.1.15.4）：13:29:14 握手成功（session=482049a007a4ce3f，mod=0.2.0 game=1.1.15.4 protocol=1）；菜单态快照 59.5–60.9Hz；用户手动开局后状态行实测 `第 1 波 · combat · 剩余 17s · 玩家存活 · hp=7`、`第 3 波 · ended`；单会话累计 69k+ 条快照无异常。
    4. 实机版本不匹配：临时把 `protocol.GAME_VERSION` 篡改为 `1.1.15.3` 后重启 agent，真 mod hello（game=1.1.15.4）被拒——agent 日志 `拒绝会话：版本不匹配：game_version='1.1.15.4'（需要 '1.1.15.3'）`，mod 日志 `服务端拒绝会话：{"code":"version_mismatch","message":"版本不匹配：game_version='1.1.15.4'（需要 '1.1.15.3'）"}`；agent 侧仅 1 次连接、拒绝后 mod 不再重连（设计如此）。随后已恢复常量并复跑 50 测试通过。
  - code-review（Standards/Spec 双轴子代理）修复项：ack 登记竞态；`--ping-interval 0` 时 2s 回收失效；`send_action` 未用 `ref` 参数与构造器未用版本/超时注入参数（推测性抽象，删除）；`welcome_payload` 未用 `config` 参数；CLI `to_thread(stdin)` 取消阻塞风险（改守护线程）；stop 无活动连接时仍报"已断开"；状态行未消费 `link_healthy`、断连后旧快照误导（加"上次快照："标注）；`requirements.txt` 的 unittest 命令不可用（补 `tests/__init__.py` 并修正说明）；architecture §11 目录树缺 `protocol.py`。
  - 剩余限制/待办：
    - 本环境合成鼠标/键盘输入无法被 Brotato 窗口接收（已排除 UIPI：双方均 Medium；SendInput/PostMessage/SendKeys/AttachThreadInput 均无效），实机对局由用户手动进入；票据 07 端到端验收需先解决 UI 驱动或继续人工辅助。
    - 实机拒绝测试后 mod 置 rejected 不再重连，需重启游戏才能恢复连接（协议设计）。
    - Ctrl+C 优雅退出在沙盒无法验证（`CTRL_C_EVENT` 对新建进程组不可投递，对照实验同样失败）；`quit` 正常退出已验证，守护线程改造避免潜在卡死。
    - `docs/protocol.md` §2.3 仍写"v2 起强制版本校验"，与 architecture §4.1/ADR-0005 及本票 v1 实现存在文字冲突，建议票据 05/06 一并收口；`.gitignore` 暂未含 `recordings/`（票据 03 范围）。

