# Research Brief: 移动/删除文件的紧凑操作卡片

**Depth**: focused  
**Date**: 2026-09-19

## Executive summary

移动和删除属于“结果明确、内容本身不需要阅读”的文件操作，不应复用修改文件的 Diff 展开模式。推荐保留同一套 `file-changes` 数据契约，但在前端把 `moved` / `deleted` 投影为不可展开的紧凑操作卡片：标题表达操作，卡片主体表达路径和结果，右侧表达执行状态。

仅把 `expandable` 设置为 `false` 不够：当前 `DiffTool` 在不可展开分支只渲染 `DisclosureRowStatic`，不会渲染 `changes` 中的路径摘要，因此需要增加“文件操作卡片”渲染分支。

## Local findings

- `apps/desktop/components/assistant-ui/tools/diff-tool.tsx` 当前将 `file-changes` 统一路由到 Diff 结构。
- `deleted` 已经不会渲染被删内容；`moved` 只携带源路径和目标路径，数据契约适合做紧凑卡片。
- `expandable=false` 当前会直接返回静态行，路径详情会消失；这解释了为什么不能只修改后端展示提示。
- `delete_file` 和 `move_file` 目前的展示元数据仍声明 `expand_layout="diff"`、`expandable=true`，所以它们仍处于 Diff 组件语义下。

## Recommended interaction

### Single file

```text
┌  删除文件                                      1 个文件   ✓ 已完成 ┐
│  [已删除]  tool_check_tmp/beta.txt                         │
└────────────────────────────────────────────────────────────┘

┌  移动文件                                      1 个文件   ✓ 已完成 ┐
│  [已移动]  tool_check_tmp/alpha.txt  →  tool_check_tmp/beta.txt │
└────────────────────────────────────────────────────────────┘
```

### Rules

1. 卡片不显示折叠箭头，也不响应点击展开。
2. 标题只出现一次；不要再在主体底部重复“已删除：”或“已移动：”。
3. 用文字状态徽标表达结果，颜色只做辅助：删除使用红色、移动使用蓝色；不要只依赖颜色。
4. 路径是主体信息，使用单行省略并提供原始路径的 `title`/可访问名称；移动使用稳定的箭头分隔源路径和目标路径。
5. 纯移动/删除不显示 `+0`、`−0`，也不显示“没有文本差异”。
6. 运行中显示 `正在删除` / `正在移动` 和 spinner；完成后切换为 `已完成`，失败时保留路径并显示受控的失败提示。
7. 多文件时卡片显示总数和前 2–3 条路径，其余显示“另有 N 个文件”；不要在卡片内再嵌套一个展开器。

## Implementation boundary

- 新增 `FileOperationCard`（或等价的纯展示组件），只消费现有 `file-changes` 的 `deleted` / `moved` 数据。
- `DiffTool` 继续负责 `added` / `modified` 等需要 Diff 的变更；不要在它内部增加按工具名的特例。
- `routeToolPart` 仍按 `data.kind` 和展示布局路由；如果需要区分布局，优先扩展通用 `expand_layout` 语义，而不是写 `delete_file` / `move_file` 工具名分支。
- 保持后端 `display_data` 不带删除文件正文；移动只传递 rename 元数据。
- 为单文件、多文件、运行中、失败、长路径和无目标路径补充组件测试。

## Research signals

- Fluent 2 将 Card 定义为承载单一对象相关信息和操作的容器，并建议内容保持简短、可扫读、直接支持行动。[Card](https://fluent2.microsoft.design/components/web/react/core/card/usage)
- Fluent 2 建议 Toast 用于操作状态确认，Message bar 用于容器状态；成功反馈应具体描述发生了什么，而不是重复“成功”。[Toast](https://fluent2.microsoft.design/components/web/react/core/toast/usage)、[Message bar](https://fluent2.microsoft.design/components/web/react/core/messagebar/usage)
- Apple HIG 建议常见且可撤销的删除不使用打断式 Alert；不可撤销的破坏性操作才需要确认。[Alerts](https://developer.apple.com/design/human-interface-guidelines/alerts)
- Apple HIG 建议破坏性按钮不承担默认主按钮角色，避免用户因视觉突出而误触。[Buttons](https://developer.apple.com/design/human-interface-guidelines/buttons)

## Suggested phases

1. 先实现卡片视觉和状态矩阵，不改变后端数据契约。
2. 将移动/删除路由到卡片，保留修改类文件的 Diff 展示。
3. 增加多文件压缩摘要、长路径可访问文本和失败状态测试。
4. 最后评估是否需要 Undo；如果当前删除不可恢复，Undo 应作为后续能力，而不是把确认弹窗塞进每张结果卡。
