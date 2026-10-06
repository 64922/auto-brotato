# Issue tracker：本地 Markdown

本仓库的 issue 与 PRD 以 markdown 文件形式存放在 `.scratch/` 下。

## 约定

- 一个功能一个目录：`.scratch/<feature-slug>/`
- PRD 为 `.scratch/<feature-slug>/PRD.md`
- 实现类 issue 为 `.scratch/<feature-slug>/issues/<NN>-<slug>.md`，从 `01` 开始编号
- triage 状态记录在每个 issue 文件顶部的 `Status:` 行（角色字符串见 `triage-labels.md`）
- 评论与对话历史追加在文件底部 `## Comments` 标题下

## 当技能要求「publish to the issue tracker」时

在 `.scratch/<feature-slug>/` 下新建文件（目录不存在则创建）。

## 当技能要求「fetch the relevant ticket」时

读取所引用路径的文件。用户通常会直接给出路径或 issue 编号。

## Wayfinding operations

供 `/wayfinder` 使用。**map** 是一个文件，每个 ticket 对应一个 **child** 文件。

- **Map**：`.scratch/<effort>/map.md` —— 承载 Notes / Decisions-so-far / Fog 正文。
- **Child ticket**：`.scratch/<effort>/issues/NN-<slug>.md`，从 `01` 开始编号，问题写在正文中。`Type:` 行记录 ticket 类型（`research`/`prototype`/`grilling`/`task`）；`Status:` 行记录 `claimed`/`resolved`。
- **Blocking**：顶部附近的 `Blocked by: NN, NN` 行。当它列出的每个文件都是 `resolved` 时，该 ticket 即解除阻塞。
- **Frontier**：扫描 `.scratch/<effort>/issues/`，找未关闭、未阻塞、未被认领的文件；编号最小者优先。
- **Claim**：开始任何工作前，设 `Status: claimed` 并保存。
- **Resolve**：在 `## Answer` 标题下追加答案，设 `Status: resolved`，然后把上下文指针（要点 + 链接）追加到 `map.md` 的 Decisions-so-far。
