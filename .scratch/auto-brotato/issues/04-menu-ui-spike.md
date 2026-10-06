# 票据 04：菜单 UI 结构 spike（难度页 / 升级选卡 / 终局）

- Status: ready-for-agent（含少量人工配合步骤）
- Blocked by: 01
- 关联：[protocol.md §7](../../../docs/protocol.md)、架构 §6.2、R3

## 背景

P1 的菜单观测与动作依赖游戏内部 UI 结构，但难度页（滑杆）、升级选卡、终局页的节点/字段/信号尚未实测。本票据先做一次性探针，产出可靠结论后再写正式代码。

## 目标

1. 写一个临时探针（mod 内调试代码或独立小脚本），在目标场景出现时把节点树/关键属性 dump 到日志或 IPC：
   - **难度选择页**（`ui/menus/run/difficulty_selection`）：
     - 场景根脚本类与方法；滑杆容器与加减按钮的节点路径；
     - 当前值、上限、解锁集合的读取方式（`difficulty_selected_value` / `difficulty_unlocked` / 存档字段）；
     - 开始按钮（`can_start` 的判定依据）；
     - 英雄与初始武器的读取路径（与 `character_selection` / `character_selection_inventory` 的关系）；
     - 模式开关（endless/ban/zone）在何处可读。
   - **升级选卡页**：场景/容器节点；卡片数据结构（id/tier/type 字段）；可用信号（选择动作等价路径）。
   - **终局页**：胜利/失败的判定信号或节点；结算统计的可读字段。
2. 人工配合：进入对应界面（选好英雄/武器停在难度页；打 1 波触发升级；正常/死亡各一次终局）。
3. 产出 `docs/protocol.md §7` 修订稿（字段定型）与本票据 Comments 结论。

## 验收

- [ ] 三个场景的节点路径/字段/信号写入票据结论，含实机证据（日志片段）
- [ ] 明确 `menu_set_difficulty` 的可行实现路径（按钮步进 vs 其它）
- [ ] 不确定项全部列出（若无则写"无"）

## 备注

- 探针代码不进入正式 mod 发布；结论先行、实现后置（票据 05/06）。
- 若某读取路径不稳定，准备双路径（`%UniqueName` + 方法回退，ADR-0006）。
