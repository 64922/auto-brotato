# 票据 03：回放录制与离线回放工具骨架

- Status: ready-for-human
- Blocked by: 02
- 关联：ADR-0008、[strategy.md §8](../../../docs/strategy.md)

## 背景

真实对局 20–30 分钟，决策迭代必须靠回放。agent 在运行中同步落盘 NDJSON。

## 目标

1. `agent/ab_agent/recorder.py`：录制文件（默认 `recordings/<时间戳>-<英雄>-<难度>.ndjson`）：
   - header 行：`{kind:"header", agent_version, mod_version, protocol_version, game_version, hero_id, weapons, difficulty, window:{...}}`；
   - 数据行：`{kind:"in"/"out", ts, envelope}`（mod→agent 全部消息、agent→mod 全部动作）；
   - `{kind:"decision", ts, layer, action, reason}`：关键决策理由（P2/P3 接入；先留接口）。
2. `tools/replay/`：读取录制文件，按时间轴重放 `in` 消息喂给决策引擎（接口先留空实现），支持 `--speed`、`--seek`、`--summary`（节点统计）。
3. 录制开关与体积控制（默认开启、可选压缩归档）；`recordings/` 不入库。

## 验收

- [x] 一次实机运行产出完整回放（可独立解析，含 header 与环境字段）
- [x] `--summary` 输出消息计数、时长、波次范围、快照频率统计
- [x] 回放重放不依赖游戏（纯文件）

## 备注

- 录制应尽量原位不阻塞 IPC 读取（异步写或队列）；性能开销以不影响 60Hz 链路为准。

## Comments

- 2026-10-06：票据 03 实现完成，保留分支 `ticket/03-replay-recorder` 与 worktree `C:\Users\33755\Desktop\auto-brotato-t03` 供独立验收（未合并、未推送）。基线 main=de5b8a9；前置依赖票据 02 已核对（依赖实现即 main 的 de5b8a9，验收项全勾）。
  - 交付物：`agent/ab_agent/recorder.py`（新模块）；`agent/ab_agent/ipc_server.py`（recorder 注入、`Envelope.as_dict()` 转发、连接替换防护）；`agent/ab_agent/protocol.py`（新增浅拷贝 `Envelope.as_dict()`）；`agent/ab_agent/cli.py`（录制开关/目录/压缩参数）；`agent/tests/test_recorder.py`（单元 + IPC 集成）、`agent/tests/test_protocol.py`（as_dict 用例）；`tools/__init__.py`、`tools/replay/{__init__,__main__,recording,engine,replay}.py`、`tools/replay/tests/{support,test_recording,test_replay}.py`；`.gitignore` 增加 `recordings/`。
  - 实现说明/接口变化：
    - `Recorder`（位置参数 `directory`，可选 `compress/logger/clock/queue_max`）：`start_session(hello, agent_version, owner=None, hero_id=None, weapons=None, difficulty=None, window=None) -> Path|None`、`record_in(envelope)`、`record_out(envelope)`、`record_decision(layer, action, reason)`、`end_session(owner=None)`、`recording_path`、`dropped`。稳态写入走有界队列（1024）+ 后台守护线程：`record_*` 仅入队（实测 0.001ms/条）；队列满或写入失败时丢弃并计数（`dropped`）、日志告警，内存有上限；`compress=True` 直接写 `.ndjson.gz`；目录/文件打不开时本会话不录制（返回 None），录制故障不影响 IPC。
    - 文件格式：header 含 `agent_version/mod_version/protocol_version/game_version` 与扩展字段 `ts/session_id`，`hero_id/weapons/difficulty/window` 本票恒 null（接口已留）；`{kind:"in",ts,envelope}`=mod→agent 全部已解析消息（含 hello；握手前被忽略/解析失败行不录）；`{kind:"out",ts,envelope}`=action 原文（与线上编码共用同一 `ts`）；`{kind:"decision",ts,layer,action,reason}` 已留接口（layer/reason 写入时强制转 str）。
    - `IpcServer(recorder=...)`：握手成功开新会话并记录 hello；每次断连/新连接替换/服务端关机都会按 owner 结束对应会话（旧连接迟到清理不会误关新会话）；`_pump` 增加"旧连接已被替换即停止处理"防护；发送动作记录实际信封。
    - CLI：录制默认开启，默认目录 `<仓库>/recordings`；新增 `--record-dir`、`--no-record`、`--record-compress`。
    - `tools/replay`：入口 `python -m tools.replay <file> [--summary] [--speed N] [--seek S]`；流式逐行解析（不整体载入内存），支持 `.ndjson`/`.ndjson.gz`，结构非法给出中文行号错误；`DecisionEngine` 协议 + `NullDecisionEngine` 占位（票据 09–12 接入真实引擎）；`--summary` 输出消息计数/分类、时长、波次范围、快照频率（平均 Hz 与间隔 min/中位/p95/max）；`--speed 0`=不等待、`--seek` 从首条 in 起跳秒。
  - 验证结果：
    1. 测试：`python -m pytest agent/tests tools/replay/tests -q` → **87 passed**；`cd agent && python -m unittest discover -s tests -t .` → **OK（61）**。覆盖：header/文件名/压缩/固定时钟防覆盖/结束后忽略/批量刷盘/目录失败降级/有界队列丢弃计数；IPC 集成（握手开会话、记录 hello+snapshot+ack、out 动作、断连关文件）；录制解析校验（header 必需、坏行行号、未知 kind、缺 envelope、decision 字段、gz、重复 header）；重放（仅喂 in、顺序、seek 跳过与越界、注入时钟验证 speed 延时、speed=0 不 sleep、CLI 退出码）。
    2. 实机（mod 0.2.0 / Brotato 1.1.15.4，session=5fd050e8dcf6909d）：127.5s（14:10:40–14:12:48）产出 `recordings/20261006-141040-unknown-unknown.ndjson` 7.08MB / 7705 行（header 1 + snapshot 7564 + shop 14 + pong 125 + hello 1），录制器无丢弃/写入失败告警；用户手动进入 D0 游侠对局并打完第 1–2 波。
    3. `--summary`：in=7704（snapshot=7564）· out=0（观察态未发动作）· 时长 127.5s · 波次范围第 1–2 波 · 快照平均 59.3Hz，间隔 min/中位/p95/max=1.0/15.0/21.7/402.2ms（402ms 为进入对局的加载间隙）。
    4. 独立解析：另用标准库脚本逐行 `json.loads` + 结构断言（不 import recorder/replay），7705 行全部通过，header 环境字段齐全（hero 等本票为 null）。
    5. 纯文件重放（agent 与游戏均已退出）：`--speed 0` 全量喂 7704 条 in 消息耗时 0.21s；`--seek 127`/`--seek 127.4` 分别喂 35/9 条。
    6. 性能：离线 600 条 × 68.5KB 快照（60Hz×10s）入队 0.7ms（0.001ms/条），end_session 排空+关闭 0.56s；gzip 压缩 40.1MiB→3.5MiB（约 11.4x）。实机链路 59.3Hz、零丢弃，未影响 60Hz。
  - code-review（Standards/Spec 双轴子代理）修复项：有界队列+丢弃计数与结束告警；去除事件循环内 `dataclasses.asdict` 深拷贝（改 `Envelope.as_dict()` 浅拷贝）；out 信封 `ts` 与线上编码一致；连接替换竞态防护（`_pump` 停止处理旧连接）；`record_decision` 的 layer/reason 强制 str 保证 replay 严格解析；replay 解析器补"首行必须 header"校验；移除未用常量。
  - 剩余限制/待办：
    - header 中 `hero_id/weapons/difficulty/window` 本票恒为 null、文件名为 `unknown-unknown`：协议 v1 无菜单观测；票据 05/06 实现菜单观测后由调用方在 `start_session` 传入（接口已就绪，届时需补"握手后补充 header 上下文"的机制，或在菜单消息首达时开录）。
    - `out` 仅记录 `action`；welcome/ping/error 不入录制（按工单"agent→mod 全部动作"）。观察态（OBSERVE_ONLY）无动作，故本次实机 out=0。
    - 握手前被忽略的消息与解析失败行不录制（hello 已录）。
    - 录制无轮转/大小上限：满负载约 4MB/s，长局建议 `--record-compress`（实测约 11.4x）；文件巨大时 `--summary` 为单次流式扫描，内存与文件行数无关。
    - 实机回放文件保留在 worktree `recordings/`（已 gitignore）供独立验收使用。
