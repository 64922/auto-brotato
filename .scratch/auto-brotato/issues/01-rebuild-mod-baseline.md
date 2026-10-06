# 票据 01：重建 mod 源码基线并建立部署工具链

- Status: ready-for-agent
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

- [ ] `mod/` 与 zip 内容一致（可 diff）
- [ ] 部署脚本一键执行成功，重复执行安全（幂等、有备份）
- [ ] 冒烟四项全部通过，结论记录在票据 Comments
- [ ] 确认 workshop 目录覆盖风险（R1）：记录 Steam 是否会覆盖该目录的行为

## 备注

- 既有 `ipc_client.gd` 默认端口 37650、协议 v1；不修改协议。
- 若 Steam 会覆盖 workshop 目录，评估备选加载路径（游戏目录 mods / 用户目录 mods），但不改变"部署脚本可重建"的结论。
