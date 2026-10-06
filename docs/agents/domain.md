# Domain Docs

工程技能在探索代码库时应如何消费本仓库的领域文档。

## 探索之前，先读这些

- 仓库根目录的 **`CONTEXT.md`**，或
- 仓库根目录的 **`CONTEXT-MAP.md`**（如果存在）—— 它指向每个上下文各自的 `CONTEXT.md`。读取与当前主题相关的每一份。
- **`docs/adr/`** —— 阅读涉及即将改动区域的 ADR。多上下文仓库还需检查 `src/<context>/docs/adr/` 中的上下文级决策。

如果这些文件不存在，**静默继续**。不要指出其缺失，也不要提议预先创建。`/domain-modeling` 技能（经由 `/grill-with-docs` 和 `/improve-codebase-architecture` 到达）会在术语或决策真正落地时惰性创建它们。

## 文件结构

本仓库为 single-context（单一上下文）：

```
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-event-sourced-orders.md
│   └── 0002-postgres-for-write-model.md
└── src/
```

作为参照，multi-context（多上下文）仓库长这样（以根目录存在 `CONTEXT-MAP.md` 为标志）：

```
/
├── CONTEXT-MAP.md
├── docs/adr/                          ← 系统级决策
└── src/
    ├── ordering/
    │   ├── CONTEXT.md
    │   └── docs/adr/                  ← 上下文级决策
    └── billing/
        ├── CONTEXT.md
        └── docs/adr/
```

## 使用词汇表的术语

当你的输出命名某个领域概念时（issue 标题、重构提案、假设、测试名称），使用 `CONTEXT.md` 中定义的术语。不要漂移到词汇表明确回避的同义词。

如果你需要的概念还不在词汇表中，这是一个信号——要么你在发明项目不使用的语言（重新考虑），要么存在真实的缺口（记下来交给 `/domain-modeling`）。

## 标注 ADR 冲突

如果你的输出与现有 ADR 矛盾，显式指出而不是悄悄覆盖：

> _与 ADR-0007（event-sourced orders）冲突 —— 但值得重新讨论，因为……_
