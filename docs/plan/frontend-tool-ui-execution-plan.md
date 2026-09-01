# 前端工具调用 UI 改造方案

## 目标

工具执行前立即显示“将执行什么工具、目标和参数”；工具结束后，在同一个 tool-call UI 中回显结构化结果、失败原因或取消状态。前端只负责渲染和交互，不执行工具、不持有对话事实。

## 代码事实

- 前端运行时边界是 `useAssistantTransportRuntime`。
- `apps/desktop/lib/assistant/converter.ts` 已将后端工具 part 映射为 assistant-ui `tool-call` part。
- `apps/desktop/components/assistant-ui/elements/tool-fallback.aui.tsx` 已能兜底展示结构化结果。
- 后端 `ToolObservation.data` 是面向客户端的结构化结果，前端不应从 `content` 文本猜测数据。
- 当前桌面端已安装 `@assistant-ui/react@0.15.17` 与 `@assistant-ui/react-markdown@0.14.13`；没有独立的官方 elements 包，`ToolGroup` 与 `ToolFallback` 是本地组件。
- 后端 Assistant Transport 传输的是完整 canonical state 快照；工具开始事实由 `create_tool_call` 提交后才可见，当前没有逐 token 的原始 `argsText` 工具参数流。
- 当前 `AssistantRuntimeProvider` 尚未配置 `AuiConfig({ tools: Tools({ toolkit }) })`；仅新增 renderer 文件而不注册 toolkit，不会让 `part.toolUI` 生效。

## 推荐架构

```text
AssistantRuntimeProvider
└── useAssistantTransportRuntime
    └── Thread
        └── MessagePrimitive.Parts
            └── ToolGroup（连续调用折叠）
                └── backend toolkit renderers
                    ├── ToolCall（通用生命周期壳）
                    ├── TerminalBlock
                    ├── CodeDiff / ReviewableDiff
                    ├── FileTree / SearchResults
                    ├── WebSearch / Sources
                    └── ToolFallback（未知工具兜底）
```

工具 renderer 注册为 `type: "backend"`，只提供 `render`，不提供前端 `execute`，并通过 `AuiConfig({ tools: Tools({ toolkit }) })` 注入 `AssistantRuntimeProvider`。这符合项目“前端不得执行工具”的架构决议，也符合 Assistant UI 的 [Tool UI](https://www.assistant-ui.com/docs/tools/tool-ui) 模式。renderer 选择依据是 `toolName`；`data.type` 属于独立的 data part，不能作为 tool-call renderer 的选择条件。

## 生命周期显示

| 状态 | 前端表现 |
|---|---|
| canonical `pending` / `running` | 在工具开始事实提交后的下一份 state 快照中显示工具名、目标、已解析参数与 spinner；这不是当前的逐 token 原始参数流 |
| canonical `completed` | check、耗时/摘要、结构化结果 |
| canonical `failed` | `isError: true`，由 renderer 根据明确的错误显示字段展示失败原因；不能只依赖 `status.type === "incomplete"` |
| canonical `cancelled` | 中性取消状态，不显示为红色失败；取消时不能把占位结果误当成成功结果 |
| `requires-action` | 只有后端投影 `approval`/`interrupt` 且提供恢复命令/端点后，才显示 approval/human input UI |

连续工具调用直接复用当前 `Thread` 中已经接入的 `MessagePrimitive.GroupedParts` 分组逻辑；本地 `ToolGroupRoot` / `ToolGroupTrigger` / `ToolGroupContent` 仅作为组合实现参考，不应再绕过 `Thread` 建第二条分组链。需要独立展示的 renderer 使用 toolkit 的 `display: "standalone"`，不按工具类别手写分组规则。实现形态参考官方 [Tool group](https://www.assistant-ui.com/elements/tool-group)。官方文档没有独立的 `/elements/tool-call` 页面，通用生命周期应参考 [Tool UI](https://www.assistant-ui.com/docs/tools/tool-ui)。

## 工具 renderer 映射

| 工具 | renderer | 结果布局 |
|---|---|---|
| `list_directory` | DirectoryToolUI | File tree / list |
| `read_file` | ReadFileToolUI | 文件信息 + code block |
| `search_files` | SearchFilesToolUI | 文件分组、行号、上下文 |
| `codegraph_*` | CodegraphToolUI | 节点/关系；复杂图进入 Canvas/Flow graph |
| `apply_patch`、`patch` | PatchToolUI | Code diff |
| `write_file` | WriteFileToolUI | 文件摘要 + Code diff |
| `delete` | DeleteToolUI | 高风险目标清单 + 快照/撤销状态 |
| `execute_terminal` | TerminalToolUI | Terminal block |
| `web_search` | WebSearchToolUI | 搜索结果 + Sources |
| `web_extract` | WebExtractToolUI | URL、标题、正文摘要 |
| `delegate_task` | DelegateTaskToolUI | Subagent list |
| 未注册工具 | ToolFallback | 参数 + JSON/文本结果 |

优先复用官方元素的实现契约与视觉结构：[Terminal block](https://www.assistant-ui.com/elements/terminal-block)、[Code diff](https://www.assistant-ui.com/elements/code-diff)、[Reviewable diff](https://www.assistant-ui.com/elements/reviewable-diff)、[File tree](https://www.assistant-ui.com/elements/file-tree)。这些页面提供可复制到本地的组件实现，不等于当前已安装的运行时依赖；落地时应维护当前项目的本地组件，或在明确评估后把官方 registry 源码作为项目代码纳入版本控制，不能直接假定存在可 import 的独立包。`WebSearchToolUI`、`DelegateTaskToolUI` 等需先确认后端结果 schema，不能仅凭工具名假定存在。

## 协议前置条件与状态映射

Assistant Transport 的 state converter 每次接收完整快照；同一 `toolCallId` 能否更新为同一 UI，取决于后端在同一个 message part 位置持续投影同一调用，而不是通过前端 event store 拼卡片。当前后端的 `ConversationStateService` 与 `ConversationToolCallModel` 已满足这一点：开始时插入调用事实，完成时原地更新结果/状态，revision 变化后重新投影。

但 `@assistant-ui/react@0.15.17` 的 `ToolCallMessagePart` 没有可直接写入的后端生命周期字符串。其 part status 会由 runtime 推导：工具 `result` 缺失时跟随 assistant message status；有 `result` 时归一为 `complete`。因此 converter 必须定义并测试一个明确的显示映射：

1. `pending` / `running`：保留 `args`，不填 `result`；父 assistant message 为 running，使 renderer 得到 running。
2. `completed`：保留 `result` 为 `ToolObservation.data`，不字符串化。
3. `failed`：设置 `isError: true`，并由 converter 把 canonical `error` 与可用的 `data` 组合成明确的错误 result envelope（例如 `{ kind: "tool-error", message, data }`）供 renderer 展示；renderer 必须优先判断 `isError`，不能期待有独立的 `incomplete + error` tool status。这个 envelope 是展示投影，不得被当作成功结果或写回服务端。
4. `cancelled`：不得沿用后端为兼容模型而写入的空对象作为成功 result；converter 应提供明确的取消显示投影（例如 `{ kind: "tool-cancelled" }`），并让 renderer 优先依据 canonical 状态/该投影显示中性取消。若要求工具级取消而父 message 仍为 completed，必须先扩展 transport/display contract，再实现 UI。
5. `requires-action`：当前后端虽有 LangGraph interrupt/resume 编排，但 Assistant Transport 请求只支持 `add-message`，state projection 也未投影 `approval`/`interrupt`。approval UI 只能作为后续阶段，必须先补 canonical request、state 和恢复闭环，不能把现有 fallback 按钮误当成已接通的服务端审批。

工具失败时现有 `ToolFallback` 只在 `status.type === "incomplete"` 时显示错误，而当前 converter 将错误放入 `result` 后会得到 complete；因此本方案落地必须同步修改 fallback/各 renderer 的错误分支，并补充 converter 单测覆盖成功、失败、取消和同 `toolCallId` 更新。

## 前端改动范围

1. 在 assistant UI 边界新增 toolkit registry，按 `toolName` 选择 renderer，并在 `AssistantRuntimeProvider` 注册 `Tools`；不在前端声明 `execute`。
2. converter 保留 `toolCallId`、解析后的 `args`、结构化 `result`、`isError` 和 UI-only 错误/取消显示字段；`argsText` 只能由真实原始参数流提供，不能把 `JSON.stringify(args)` 宣称为原始流。
3. 复用当前 `Thread` 的 `GroupedParts` 通用折叠能力；官方 elements 只作为实现参考或明确纳入项目的本地组件，不假设存在可直接 import 的独立包。
4. renderer 只负责展示，不新增工具执行、数据库写入或本地事实存储。
5. turn 级失败使用 `ErrorPrimitive` + `ActionBarPrimitive.Reload`；工具级失败由工具 renderer 展示。
6. 大文件、Diff、CodeGraph 使用折叠、延迟渲染和 Canvas split，避免一次挂载大量 DOM。

## 前端验收

- 工具开始事实提交后、handler 完成前已有可见工具行；若产品要求模型产生 tool call 的瞬间即显示，则需另增 backend tool-call-start 事实/投影，不属于当前方案已有能力。
- 同一 `toolCallId` 的 UI 从 running 更新为结果，不产生重复卡片。
- 成功、失败、取消状态明显区分；审批等待只有在 approval/interrupt 投影与恢复命令闭环完成后才列入验收。
- `data` 为对象时不会出现 `[object Object]`。
- 未知工具能由 `ToolFallback` 展示。
- 刷新/恢复历史时工具调用和结果可重放。
- 构建、TypeScript 和 Assistant Transport 回归测试通过。

## 审查结论（2026-09-01）

**前端方案通过（按本次修订后的边界）。**

修改点：

- 修正 Assistant UI toolkit 注册方式，补充 `AuiConfig` / `Tools` 注入前提。
- 移除无效的 `/elements/tool-call` 引用，区分官方文档参考与项目已安装依赖/本地组件。
- 将 renderer 选择条件从 `toolName`/`data.type` 修正为 `toolName`。
- 把 Assistant Transport 的“完整快照 + 同 `toolCallId` 原地更新”写成可验收协议。
- 修正工具失败/取消的状态语义：不再假设 `incomplete + error` 会由 `isError` 自动产生，要求 converter 与 renderer 使用明确的错误/取消显示字段。
- 明确当前没有逐 token 原始 `argsText` 流，验收改为“开始事实提交后显示已解析参数”；真实参数流列为后续协议扩展。
- 将 approval/human input 标为后续前后端闭环，不再把当前 fallback 按钮描述为已接通审批。

风险：

- 工具开始事实提交存在数据库与 state subscription 的调度延迟，无法承诺 handler 调用前零延迟显示。
- 若后端继续对失败/取消写入空对象 result，前端会误判为 complete；这是必须由 converter 与 transport contract 一起防住的回归点。
- 官方 elements 页面是实现参考/registry 组件来源，不是当前 package.json 中的依赖；直接照搬 import 路径会导致构建失败。
- `ToolObservation.data` 的各工具 schema 尚未全部形成前端类型契约，专用 renderer 在 schema 固化前只能保留 fallback。
- approval/resume、工具级取消和真实参数增量流需要后端契约扩展，不能仅靠前端 renderer 补齐。

## 不采用

- 不重新引入旧 SSE、event store 或客户端工具执行链。
- 不按每个工具手写独立的折叠、加载、错误、JSON、Diff 基础组件。
- 不从 `content` 文本正则解析文件、命令、exit code 或 diff。
