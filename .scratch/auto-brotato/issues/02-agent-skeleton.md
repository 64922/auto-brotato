# 票据 02：Python agent 骨架（IPC 服务端 + 状态模型 + CLI 骨架）

- Status: ready-for-agent
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

- [ ] `conda activate brotato` 后直接运行，无第三方缺失依赖
- [ ] 与票据 01 部署的 mod 实机握手成功，终端显示快照频率与波次
- [ ] 版本不匹配（临时篡改常量模拟）时拒绝会话并给出清晰提示
- [ ] 单元测试通过（pytest 或 unittest）

## 备注

- 单客户端假设：仅接受一个活动连接；重复连接时替换旧连接并告警。
- 不做决策逻辑；不做回放（票据 03）。
