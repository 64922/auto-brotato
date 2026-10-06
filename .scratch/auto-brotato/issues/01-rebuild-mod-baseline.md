# 票据 01：重建 mod 源码基线并建立部署工具链

- Status: ready-for-human
- Blocked by: 无（P0，最先做）
- 关联：[architecture.md §11](../../../docs/architecture.md)、[protocol.md](../../../docs/protocol.md)、ADR-0001

## 背景

AutoBrotato mod 0.2.0 已实机验证，但源码唯一副本是 workshop zip：
`E:\SteamLibrary\steamapps\workshop\content\1942280\3814007307\BrotatoPlayer-AutoBrotato-0.2.0.zip`
（结构 `mods-unpacked/BrotatoPlayer-AutoBrotato/{manifest.json, mod_main.gd, src/*.gd}`）。

## 目标

1. 将 zip 源码提取到本仓库 `mod/`（保留 mods-unpacked 结构或调整后同步更新部署脚本），并提交入库（源码安全第一）。
2. 建立 `tools/deploy_mod.ps1`（或 Python 等价物）：
   - 打包 `mod/` 为 `BrotatoPlayer-AutoBrotato-<version>.zip`；
   - 备份既有 zip 与 `%APPDATA%\Brotato\` 相关配置（backups/ 带时间戳）；
   - 写入 `E:\SteamLibrary\steamapps\workshop\content\1942280\3814007307\`；
   - 更新 `%APPDATA%\Brotato\auto_brotato_deploy.json`（沿用既有字段）。
3. 冒烟验证：重启游戏 → ModLoader 日志加载成功 → hello/welcome 握手（用最简 TCP 工具或票据 02 的 agent）→ 快照上送 → move 注入实机移动、断连 TTL 停住。

## 验收

- [x] `mod/` 与 zip 内容一致（可 diff）
- [x] 部署脚本一键执行成功，重复执行安全（幂等、有备份）
- [x] 冒烟四项全部通过，结论记录在票据 Comments
- [x] 确认 workshop 目录覆盖风险（R1）：记录 Steam 是否会覆盖该目录的行为

## 备注

- 既有 `ipc_client.gd` 默认端口 37650、协议 v1；不修改协议。
- 若 Steam 会覆盖 workshop 目录，评估备选加载路径（游戏目录 mods / 用户目录 mods），但不改变"部署脚本可重建"的结论。

## Comments

- 2026-10-06：mod 0.2.0 源码已从 workshop zip 提取入仓 `mod/`（manifest.json + mod_main.gd + src/ 共 9 个文件，SHA256 与 zip 逐一比对一致）。剩余：`tools/deploy_mod.ps1` 与四项实机冒烟。
- 2026-10-06：票据 01 实现完成，保留分支 `ticket/01-rebuild-mod-baseline` 与 worktree 供独立验收（未合并、未推送）。
  - 交付物：`mod/`（9 文件）；`tools/deploy_mod.ps1`（PowerShell 5.1，UTF-8 BOM）；`tools/smoke_mod.py`（仅标准库的临时冒烟客户端，票据 02 的 agent 落地后即被取代）。
  - 部署脚本验证：沙盒 4 场景（全新部署 / 重复执行 / 同秒重跑 / 旧版本 zip 清理）全部通过；真实部署一次，备份至 `%APPDATA%\Brotato\backups\20261006-123502`，产物写入 `E:\SteamLibrary\steamapps\workshop\content\1942280\3814007307\BrotatoPlayer-AutoBrotato-0.2.0.zip`，并更新 `mod_user_profiles.json` 与 `auto_brotato_deploy.json`（沿用既有字段 mod_id/item_id/item_dir/zip_path/profile_path/deployed_at，tab+LF、无 BOM）。重复执行幂等。
  - 冒烟四项（重启游戏后，协议 v1 端口 37650，未修改协议）：
    1. ModLoader 加载 zip：PASS（日志：BrotatoPlayer-AutoBrotato-0.2.0.zip loaded）。
    2. hello/welcome 握手：PASS（protocol=1、mod=0.2.0、game=1.1.15.4）。
    3. 快照上送：PASS（713 条 / 12.0s = 59.4Hz；wave=1、phase=combat）。
    4. move 实机位移 + 断连 TTL 停住：PASS（位移 582.1px、平均 232.9px/s、方向 cos=1.00；停止注入后最大速度 0.0px/s，且停住窗口内 player.alive=true、hp=12，排除"阵亡被误判为停住"；32 条 ack 全部 ok）。
  - R1（workshop 覆盖风险）：本机实证——部署后历经游戏重启与多轮长会话，zip 持续被 ModLoader 加载，Steam 未清理该目录；`appworkshop_1942280.acf` 中该 item 的本地 manifest 与 latest 一致，说明 Steam 仅在工坊侧更新 / 取消后重新订阅等触发清单同步时才会重写目录，届时手工写入的文件可能被覆盖。结论：workshop 目录不可视为持久存储，部署必须可重建——本脚本幂等、可随时重跑，满足该结论；不改变现有加载路径。
  - 限制：低难度下对局存活窗口短（危险 0 无尽局约 5s 即阵亡），move/TTL 证据取自一次存活对局（smoke-move4.log）；开局操作依靠 UI 合成输入辅助，属环境手段、未入库，合成输入时序不稳定（首次点击常被吞，之后才能生效）。
- 2026-10-06（code-review 补充）：
  - 修正部署脚本编码缺陷：`Get-Content -Raw` 补 `-Encoding UTF8`（PowerShell 5.1 默认 ANSI 会损坏非 ASCII 的 manifest/profile 字段）；修正后沙盒复验通过（fresh/repeat 两轮 exit 0、备份清单正确、含中文 profile 名往返无损）。
  - "断连 TTL 停住"补证：在持续注入 move 期间强制终止客户端进程，modloader.log 13:18:54 记录 `IPC 断开，move 注入已归零（安全停住）`；与 smoke-move4.log 的"停发后 TTL 内 0.0px/s 且玩家存活"合为双向证据。
  - 验收 1 独立复核：`backups/20261006-123502` 中覆盖前的原始 zip 与 `mod/` 逐文件 SHA256 一致（9/9），提取保真不依赖脚本自身产物。
  - 已知继承债务（来自 0.2.0 基线；票据 01 以"保真入仓"为目标未重构，建议后续演化票据处理）：observation.gd 554 行（超 500 行指导线，非空行 496）；shop_actions 以 JSON 签名比较协议形状（协议/领域耦合）；STAT_KEYS 与 `_int_or` 在 observation/shop_observation/engine_compat 间重复；ipc_client 状态用裸字符串；shop_actions.reset() 未断开已绑定信号。
  - 范围说明：脚本额外实现 mod_user_profiles.json 激活指向、旧版本 zip 清理、备份 sha256 清单、游戏进程运行检测——均为"可重建部署"的工程保障，已在脚本 .DESCRIPTION 记录，不改变票据结论。

