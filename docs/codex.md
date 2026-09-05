结论：前端不应该为每个工具完全手写一套独立组件，而应该按照“工具结果形态”建立少量通用 UI，再由工具声明选择布局。

当前代码已经暴露了这套方向：

- [ToolDisplayHints](H:/coding-agent/apps/backend/app/core/tools/schemas/tool_display.py:16) 已有 `expandable`、`expand_layout`。
- 各工具已经产生不同的结构化数据：`entries`、`items`、`web`、`changes`、`codegraph`。
- 但当前 [toolkit.tsx](H:/coding-agent/apps/desktop/components/assistant/toolkit.tsx:12) 所有工具都使用 `ToolFallback`，所以目前前端还无法体现工具之间的差异。
- [tools_node.py](H:/coding-agent/apps/backend/app/core/workflows/nodes/tools_node.py:190) 当前主要传递 `observation.content`，应改为同时传递结构化 `observation.data`，否则 diff、网页、CodeGraph 等富 UI 无法正确渲染。

assistant-ui 官方也建议：普通工具调用折叠进 trace，需要用户关注的结果使用 standalone Tool UI；工具 UI 只负责展示，工具执行仍在后端完成。[Tool UI](https://www.assistant-ui.com/docs/tools/tool-ui)、[Message Part Grouping](https://www.assistant-ui.com/docs/guides/part-grouping)。

## 建议的通用 UI 类型

```text
CompactToolRow       一行状态，不展开
CollapsibleToolCard  标题 + 摘要，详情折叠
ListResultCard       列表结果
DiffResultCard       文件变更 / diff
TerminalResultCard   命令、退出码、终端输出
WebResultCard        网页标题、链接、摘要
DelegationCard       子 Agent 状态和结果
```

所有组件都必须处理：

```text
pending → running → completed
                    ↘ failed / cancelled
```

失败和取消状态不应被默认折叠隐藏。

## 各工具建议样式

### 1. `read_file`

当前工具返回带行号的文本，且 `ToolDisplayHints` 已明确：

```python
expandable=False
expand_layout="none"
```

建议：只显示紧凑状态行，不在工具区域直接铺满文件内容。

```text
✓ 读取文件
  apps/backend/app/core/runtime.py · 第 1–120 行 · 8.4 KB
```

如果读取被截断：

```text
✓ 读取文件
  runtime.py · 第 1–120 行
  结果已截断 · 可继续读取 offset=121
```

完整正文仍作为模型上下文使用；用户真正需要查看代码时，可通过最终回答中的代码引用或专门的文件查看能力查看。

需要补充的结构化结果：

```json
{
  "type": "text_read",
  "path": "runtime.py",
  "offset": 1,
  "limit": 120,
  "total_lines": 480,
  "file_size": 8420,
  "next_offset": 121
}
```

### 2. `list_directory`

当前已有 `data["entries"]`，并声明为 `list` 布局。

建议：普通 trace 中显示为可折叠目录列表。

```text
▸ 读取目录 · apps/backend
  42 个条目

  📁 app
  📁 tests
  📄 pyproject.toml
  🔗 current → ...
```

默认折叠，展开后显示：

- 名称
- 类型：file / dir / link
- 路径
- 分页提示

不建议把整个目录树直接展开，否则连续调用时会严重占据对话空间。

### 3. `search_files`

当前支持三种模式：

- `target="content"`：显示命中行
- `target="files"`：显示文件路径
- `output_mode="count"`：显示命中数量

建议：折叠的搜索结果卡片，默认只显示数量，不展示命中的文件内容。

```text
▸ 搜索文件 · “ToolDefinition”
  18 个命中 · 7 个文件
```

展开后：

```text
apps/backend/app/core/tools/schemas/tool_definition.py:12
> class ToolDefinition:

apps/backend/app/core/tools/tool_registry.py:8
> from ... import ToolDefinition
```

`files_only` 模式：

```text
▸ 查找文件 · *.py
  24 个文件

  apps/backend/app/core/runtime.py
  apps/backend/app/core/tools/tool_registry.py
  apps/backend/tests/test_runtime.py
```

`count` 模式：

```text
▸ 统计匹配 · “TODO”
  6 个文件 · 31 次命中

  runtime.py       12
  tool_registry.py  8
  workflow.py       5
```

### 4. `write_file`

当前已经产生文件变更结构：

```text
data["changes"]
data["diff_stats"]
```

建议：作为 standalone 的变更卡片，不放进普通工具 trace。

```text
▸ 修改文件 · 1 个文件 · +18 -4
  apps/backend/app/core/runtime.py
  已完成
```

展开后显示标准 diff：

```diff
apps/backend/app/core/runtime.py

- old line
+ new line
+ added line
```

文件状态应使用颜色区分：

```text
added    绿色
modified 黄色/蓝色
deleted  红色
moved    紫色
```

`write_file` 不应直接展示整个新文件，而应展示 diff。

### 5. `patch`

`patch` 本质上是单文件 fuzzy find-and-replace，已有 diff 结构。

建议与 `write_file` 使用同一个 `DiffResultCard`：

```text
▸ 应用修改 · runtime.py · +6 -2
  fuzzy match: success
```

展开后显示：

```diff
@@ runtime.py:120 @@

- previous implementation
+ replaced implementation
```

如果匹配失败，不显示空 diff，而是显示明确错误：

```text
✗ 修改失败 · runtime.py
  未找到匹配文本
  展开查看修复建议
```

### 6. `apply_patch`

`apply_patch` 支持多文件修改，因此适合更明显的 standalone 变更卡片。

```text
▸ 应用补丁 · 4 个文件 · +42 -17
  ✓ runtime.py       +12 -4
  ✓ tool_registry.py +18 -8
  ✓ workflow.py      +9 -3
  ✓ tests/test_x.py  +3 -2
```

展开后：

```text
[ runtime.py ]       ▾
[ tool_registry.py ] ▸
[ workflow.py ]      ▸
[ tests/test_x.py ]  ▸
```

不要一次展开所有文件 diff，默认只展开第一个文件或全部折叠。

### 7. `delete`

当前已经设置：

```python
expandable=False
```

而且删除目录、链接时不一定有结构化 `changes`。

建议：始终显示为 standalone 的危险操作状态行，但不默认展示删除前全文。

```text
✓ 删除文件
  apps/backend/temp/example.py
```

目录：

```text
✓ 删除目录
  apps/backend/temp · recursive
```

失败时突出显示：

```text
✗ 删除失败
  目录非空，需要 recursive=true
```

如果是文件删除且存在 `changes`，可以提供可选的删除 diff：

```text
▸ 删除文件 · example.py
  - 旧文件内容
```

但不建议把删除前全文作为默认内容展示。

### 8. `execute_terminal`

当前工具具有：

- 高风险等级
- 命令级 timeout
- exit code
- timed out
- output truncated
- ANSI 清理和输出脱敏

建议使用 `TerminalResultCard`，命令始终可见，输出默认折叠。

```text
▸ 执行命令 · npm test
  exit code 0 · 12.4s
```

展开后：

```text
$ npm test

✓ 128 passed
✓ 4 files passed
```

失败：

```text
✗ 执行命令 · pytest
  exit code 1 · 8.2s
```

展开：

```text
$ pytest

FAILED tests/test_runtime.py::test_cancel
...
```

超时：

```text
✗ 命令超时 · 60s
  输出为部分结果，建议增加 timeout 或拆分命令
```

建议后端额外提供：

```json
{
  "type": "terminal",
  "command": "npm test",
  "workdir": ".",
  "output": "...",
  "exit_code": 0,
  "timed_out": false,
  "truncated": false
}
```

当前 terminal 只有文本 `content`，不利于前端可靠展示退出码和超时状态。

### 9. `web_search`

当前已有：

```json
data["web"]
```

其中包含标题、URL、描述和位置。

建议：使用 standalone 的网页结果卡片。搜索结果是用户可感知的信息，不应完全埋在工具 trace 中。

```text
▾ 网页搜索 · “assistant-ui custom transport”
  找到 5 个结果

  Assistant Transport
  assistant-ui.com
  Stream agent state to the frontend...

  Custom Runtime
  assistant-ui.com
  Build a React chat UI for any AI backend...
```

每个结果可以：

- 显示标题
- 显示域名
- 显示摘要
- 点击后在浏览器打开
- 显示搜索序号

不要直接把搜索结果转成 Markdown 正文，否则会破坏工具结果和 assistant 正文的边界。

### 10. `web_extract`

当前返回网页正文、标题、metadata 和 provider 信息，且超长正文可能落盘。

建议：使用“网页文档卡片”，默认只显示页面摘要，正文折叠。

```text
▾ 提取网页正文 · 2 个页面
  Markdown · Firecrawl

  Assistant Transport
  assistant-ui.com
  12,430 字符 · 已提取
```

展开：

```text
Assistant Transport

# Assistant Transport

...
```

多个 URL 时按页面分别折叠：

```text
▸ assistant-ui.com/docs/transport
▸ example.com/design
```

如果正文被截断：

```text
⚠ 正文过长，当前显示截断内容
  完整结果已保存到本地文件
```

不建议在前端自动加载本地完整文件；应由用户或 Agent 后续调用 `read_file`。

### 11. `codegraph_explore`

这是当前 CodeGraph 的主入口，返回相关源码和调用路径，但代码中明确说明本期不做结构化解析，会回退到 `raw`。

建议：使用 standalone 的“代码探索”卡片，默认折叠源码。

```text
▾ 代码语义查询
  发现 6 个相关文件 · 包含调用路径
```

展开后：

```text
相关文件：

apps/backend/app/core/runtime.py
apps/backend/app/core/context/runtime_context_manager.py
apps/backend/app/core/workflows/workflow_operations.py

调用路径：

runtime → workflow → tool scheduler
```

当前如果只有：

```json
{
  "codegraph": {
    "tool": "codegraph_explore",
    "raw": "..."
  }
}
```

则前端使用全文代码块 fallback，不强行解析。

### 12. `codegraph_search`

当前返回符号位置、类型和签名，适合列表展示。

```text
▸ 搜索代码符号 · “ConversationEventProjector”
  找到 3 个定义

  ConversationEventProjector  class
  apps/backend/app/assistant_transport/service/conversation_event_projector.py:71

  ConversationEventProjector  import
  apps/backend/app/assistant_transport/service/__init__.py:4
```

展开条目后显示签名：

```text
class ConversationEventProjector:
```

这是典型的 `ListResultCard`，建议放入普通工具 trace 中。

### 13. `codegraph_node`

当前结果包含：

- 当前符号自身
- `Calls`
- `Called by`
- 签名和源码位置

建议使用“符号详情卡片”。

```text
▾ 查看符号 · ConversationEventProjector
  class
  apps/backend/app/assistant_transport/service/conversation_event_projector.py:71

  Calls: 3
  Called by: 5
```

展开后分成三个区块：

```text
符号信息
  class ConversationEventProjector

调用方
  WorkflowOperations
  TransportAssistantService

被调用
  _plan_tool_created
  _plan_tool_status
```

可以支持点击路径或符号，但不要让前端直接执行工具；点击动作只应触发后端已有命令。

### 14. `codegraph_callers`

当前返回调用目标列表，包含：

- name
- kind
- filePath
- lineNumber
- edge

建议使用简单的 callers 列表：

```text
▸ 查看调用方 · _plan_tool_created
  4 个调用方

  WorkflowOperations.run
  apps/backend/app/core/workflows/workflow_operations.py:328

  ConversationEventProjector.process
  apps/backend/app/assistant_transport/service/conversation_event_projector.py:126
```

默认折叠，展开显示完整列表。

### 15. `codegraph_callees`

与 callers 相反，建议使用相同组件，但标题和方向不同：

```text
▸ 查看依赖调用 · _execute_tool_call
  6 个被调用符号

  ToolExecutor.execute
  ToolTraceRecorder.span
  ToolObservation
```

可以在每项旁边显示调用边：

```text
ToolExecutor.execute    · direct_call
ToolTraceRecorder.span  · instrumentation
```

### 16. `codegraph_impact`

当前返回受影响符号列表，解析器目前是扁平结构，但原始结果按文件分组。

建议：使用 standalone 的“影响范围”卡片，并在前端按文件重新分组。

```text
▾ 分析变更影响 · ConversationStateSnapshot
  3 个文件 · 9 个受影响符号

  apps/backend/app/assistant_transport/
    conversation_event_projector.py
      _plan_tool_created
      _plan_tool_status

    conversation_task_snapshot_service.py
      apply_planned
      ensure_state_snapshot
```

这是比普通列表更重要的结果，适合独立展示。

### 17. `delegate_task`

当前 `delegate_task` 没有 `ToolDisplayHints`，但它与普通工具不同：

- 执行时间最长
- 可能并行执行
- 实际上代表一个子 Agent
- assistant-ui 支持在 Tool UI 中渲染嵌套 `messages`

建议使用 standalone 的 `DelegationCard`：

```text
▾ 委派子任务 · Code Review
  child agent: reviewer
  正在执行 · 24s
```

完成后：

```text
✓ 委派完成 · Code Review
  reviewer · 1 个建议 · 32s
```

展开后显示子 Agent 对话：

```text
子 Agent：reviewer

用户目标：
检查 conversation event projector 的幂等性

子 Agent 结果：
发现 2 个潜在问题...
```

assistant-ui 对这种场景推荐在工具 part 中提供 `messages`，再通过 `MessagePartPrimitive.Messages` 渲染嵌套只读对话。[Multi-Agent Chat UI](https://www.assistant-ui.com/docs/tools/multi-agent)。

当前如果后端还没有把子 Agent 消息投影到 tool-call part，则先显示：

```text
title
child_agent_id
status
result summary
```

后续再增加嵌套消息。

## 最终分组建议

```text
普通 trace 折叠组：
  read_file
  list_directory
  search_files
  codegraph_search
  codegraph_callers
  codegraph_callees
  execute_terminal

独立结果卡片：
  write_file
  patch
  apply_patch
  delete
  web_search
  web_extract
  codegraph_explore
  codegraph_node
  codegraph_impact
  delegate_task
```

## Transport 层建议

`ConversationStateToolCallPart` 建议最终包含：

```json
{
  "type": "tool-call",
  "toolCallId": "call-1",
  "toolName": "search_files",
  "status": "completed",
  "presentation": {
    "surface": "trace",
    "expandable": true,
    "layout": "list",
    "defaultOpen": false
  },
  "args": {},
  "result": {
    "items": []
  }
}
```

事件处理保持：

```text
ToolCallCreatedEvent
  → 创建 tool-call part
  → 写入 presentation

ToolCallStatusChangedEvent
  → 更新 status
  → 写入结构化 result / error

RunStatusChangedEvent
  → 收口未完成工具
```

Transport 仍只使用 `set` 和 `append-text`，不需要新增协议操作。前端的 converter 只做字段映射，具体 React 组件由 `ToolCallRenderer` 选择。

最重要的一点是：

```text
ToolDefinition.display
    决定展示语义

ToolObservation.data
    提供展示数据

ToolCallMessagePart.result
    承载前端可消费的结构化结果

Tool UI renderer
    决定具体 React 样式
```

这样既能支持每个工具不同的展示样式，又不会把 React 组件或 assistant-ui 类型泄漏到后端。