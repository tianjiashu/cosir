# 非委派、非 CodeGraph 工具 UI 改造：实现验收报告（第三轮最终复核）

> 状态：历史验收记录，仅保留审计用途。2026-09-10 已确认采用绿地简化方案，本文关于 Web
> 专用 renderer、`kind` 和站点状态 projection 的结论不再是当前实现契约。

## 第三轮最终独立验收结论（2026-09-08）

本轮重新读取了最新的开发方案、当前工作区源码和前两轮验收记录；只验收
`read_file`、`write_file`、`patch`、`apply_patch`、`search_files`、`list_directory`、
`delete`、`execute_terminal`、`web_search`、`web_extract`，明确排除 `delegate_task` 和所有
CodeGraph 工具。本轮没有修改任何源码，只更新了本验收文档。

**最终判定：PASS（本次范围内无阻塞项）。**

上一轮指出的 `web_extract` 运行中/取消状态退化为“等待中”的问题已经通过局部补丁解决；
没有发现需要结构性重做 Assistant Transport、snapshot 或 renderer 架构的问题。

### 1. 上一轮状态问题复核：已解决

当前实现把“工具整体生命周期”与“站点已完成结果”分开处理，语义如下：

1. `apps/backend/app/core/workflows/nodes/model_node.py` 在创建 tool-call 时调用
   `build_initial_tool_ui_data`，依据已校验的 `web_extract.urls` 生成
   `web-extract-status` 的 `pending` 站点列表。
2. `tools_node.py` 的 `running` 事件不携带新的 data；
   `tool_observation_dispatcher.py:_ui_data` 对 Web 工具的非 completed 事件返回 `None`，
   `conversation_event_projector.py:_plan_tool_status` 因而保留创建阶段的站点身份和站点状态。
3. `apps/desktop/components/assistant-ui/tools/types.ts:resolveWebExtractSiteStatus` 根据
   整体 `artifact.backendStatus` 做展示态覆盖：
   - `pending` / `running` 站点 + 整体 `running` → `running`；
   - `pending` / `running` 站点 + 整体 `cancelled` → `cancelled`；
   - `pending` / `running` 站点 + 整体 `failed` → `failed`；
   - 已经是 `success`、`failed` 或 `truncated` 的站点保持原状态，不被整体终态覆盖。
4. 取消路径由工具节点或 `ToolCallsSettledEvent` 收口为 tool-call 级
   `cancelled`；它不把 `cancelled` 写进站点 data，而是由 renderer 根据整体状态显示，
   因此 snapshot validator 继续只允许站点 data 的
   `pending/running/success/failed/truncated`，生命周期终态仍由 part.status 表达。
5. `web_extract_status.test.ts` 已直接覆盖 running、cancelled，以及取消/失败时保留
   `success`、`truncated` 的断言。

这解决了上一轮的两个局部问题：无新 data 时不覆盖站点列表，以及未完成站点不再错误地
显示为“等待中”。

### 2. 正文隔离与 Transport 生命周期：通过

- `tool_observation_dispatcher.py:_ui_result` 对 `web_search`、`web_extract` 不产生
  `snapshot.result`；完整提取正文只通过既有模型观察写回 RuntimeContext。
- `tool_ui_projection.py` 对 Web data 做字段 allowlist；`web_extract` 只传
  `kind/provider/sites` 及站点身份、状态和短错误码，不复制 `content`、`metadata` 或
  Provider 原始响应。
- `ConversationStateSnapshot` 对 Web part 的 data/result 做第二道校验，拒绝正文字段和
  非空 Web result。
- `apps/desktop/lib/assistant/converter.ts` 对 Web Extract data 再做最小字段清洗，且
  不把 Web 工具的 `part.result` 映射到 assistant-ui 的可见 result。失败/取消时前端产生
  的通用错误/取消标记也不包含后端正文。
- `web_extract_status_tool.tsx` 只读取站点和状态字段，没有正文展开入口；即便意外字段
  存在，也不会被 renderer 读取。

### 3. Web renderer、assistant-ui 语义和 quiet UI：通过

- `web_search` 使用 `web-search-results` 专用 renderer，可折叠显示查询、数量、标题、
  摘要和后端已返回的 URL，不在前端重新抓取。
- `web_extract` 使用 `web-extract-status` 专用 renderer，只显示网站和状态。
- `tool-part.tsx` 按稳定 `data.kind` 优先路由；未知 kind 仍安全回退到通用 renderer。
- `thread.aui.tsx` 使用 `MessagePrimitive.GroupedParts` 的 `groupBy` 读取
  `artifact.presentation.surface`：standalone 工具保持独立，普通工具进入
  `group-tool-trace`；工具组显式使用 `ToolGroupRoot variant="ghost"`。
- 这与 assistant-ui 官方 [Tool UI](https://www.assistant-ui.com/docs/tools/tool-ui)、
  [Tool Call](https://www.assistant-ui.com/elements/tool-call)、
  [Tool Group](https://www.assistant-ui.com/elements/tool-group)、
  [Part Grouping](https://www.assistant-ui.com/docs/guides/part-grouping) 和
  [Custom AssistantTransport](https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport)
  的 render-only、disclosure、ghost grouping、standalone 和 converter 边界一致。
- 当前 Thread 使用 ghost 工具组；文件/目录、Diff、终端和 Web renderer 的当前路径没有
  外层 card/border。`ToolGroup` 仍保留 outline/muted 作为未使用的兼容变体定义，但不影响
  本次实际渲染路径，属于后续可选的样式收敛，不是验收阻塞项。

### 4. 本轮测试结果

通过：

```text
后端专项：102 passed
后端本次范围 Ruff：All checks passed
前端 unit：15 个测试文件、49 tests passed
前端 build：通过（tsc --noEmit && vite build）
前端 lint：通过
```

完整后端套件：`168 passed, 2 failed`。两个失败均位于
`tests/test_context_usage_compute_listener.py`，分别是测试仍传入已删除的
`publish_event` 构造参数，以及未初始化 storage fixture；不涉及本次工具 UI、Web
projection、Transport snapshot 或 converter。

Playwright 通用 E2E：`2 passed, 11 failed`。失败均发生在通用 Task/Composer 初始化阶段，
主要表现为找不到“消息输入”，测试没有进入本次 Web tool renderer；它们属于当前工作区既有
的通用前端/E2E 基线失败，不作为本次范围的 UI bug，也没有据此修改源码。当前 E2E fixture
本身没有覆盖 `web_search` / `web_extract` 的专用 renderer，因此不能把这组结果宣称为 Web
renderer 的端到端通过。

### 5. 第三轮最终判定

| 验收项 | 判定 |
| --- | --- |
| 上一轮 `web_extract` running/cancelled 显示为 pending | 已修复 |
| pending/running 根据整体 backendStatus 显示 running/cancelled/failed | PASS |
| success/truncated 状态不被整体终态覆盖 | PASS |
| 站点身份在无新 data 的失败/取消路径保留 | PASS |
| 正文不进入 event.result、snapshot.result、snapshot.data 或 converter 可见 result | PASS |
| Web search/extract 专用 renderer 与稳定 kind 路由 | PASS |
| Assistant Transport snapshot 校验 | PASS |
| GroupedParts、ToolGroup ghost、standalone/trace 语义 | PASS |
| 当前实际工具 UI 路径无外层边框 | PASS |
| 本次范围是否存在阻塞项 | **无** |
| 是否需要结构性 UI 改造 | **否** |
| 本次实现验收 | **PASS** |

发布前仍建议在真实 Tauri WebView 中补一次 Web Search 展开、Web Extract 取消/失败和
重连恢复的人工验收；这是运行环境验证建议，不改变本轮基于代码事实和专项测试得出的
通过结论。

---

## 第二轮独立复核结论（2026-09-08）

本轮只读取上一轮报告、开发文档和当前工作区代码，并运行专项测试；除本验收文档外没有修改任何源码。验收仍只覆盖 `read_file`、`write_file`、`patch`、`apply_patch`、`search_files`、`list_directory`、`delete`、`execute_terminal`、`web_search`、`web_extract`，明确排除 `delegate_task` 和所有 CodeGraph 工具。

**最终判定：FAIL（上一轮的空 data 覆盖问题已修复，但仍有一个本次范围内的局部 UI 状态阻塞项）。**

### 1. 上一轮指出的问题：已解决

当前代码事实已经满足“失败/取消且没有新 UI data 时不覆盖创建态站点列表”：

1. `apps/backend/app/core/workflows/nodes/model_node.py` 在 `ToolCallCreatedEvent` 中通过 `build_initial_tool_ui_data` 写入 `web-extract-status`，为每个 URL 建立 `pending` 站点身份。
2. `apps/backend/app/core/workflows/nodes/helper/tool_observation_dispatcher.py:_ui_data` 对 Web 工具的非 `completed` 事件返回 `None`；因此失败/取消且没有新的 UI data 时，`ToolCallStatusChangedEvent.data` 不再携带空投影。
3. `apps/backend/app/assistant_transport/service/conversation_event_projector.py:_plan_tool_status` 仅在 `event.data is not None` 时写入 `part.data`；`None` 不会清除创建阶段的站点列表。
4. 失败/取消事件仍然写入 `part.status=failed/cancelled`，并保持 `result=None`；因此最终快照同时保留站点身份、工具终态和正文隔离。

专项测试已直接覆盖失败事件无 data 的快照断言；本轮后端专项测试结果为 `100 passed`。

### 2. 仍未通过的本次范围问题：Web Extract 逐站状态没有覆盖 running/cancelled

开发文档第 5.3、11.3 节要求同一 `web-extract-status` shape 覆盖 `pending`、`running`、`success`、`failed`、`truncated`，并要求取消时仍保留网站身份和状态。当前实现存在以下具体事实：

- `apps/backend/app/core/workflows/nodes/tools_node.py` 的 running `ToolCallStatusChangedEvent` 没有传 `data`；dispatcher 因而保留创建阶段的 `sites[*].status="pending"`，不会变成 `running`。
- `apps/backend/app/core/workflows/nodes/helper/tool_observation_dispatcher.py:_ui_data` 对 cancelled 事件同样返回 `None`。这解决了“不能用空 data 覆盖站点身份”，但没有把站点状态投影为 cancelled。
- `apps/desktop/components/assistant-ui/tools/web-extract-status-tool.tsx` 只有 `artifact.backendStatus === "failed"` 时才覆盖站点状态；总体状态为 `cancelled` 时仍把站点的 `pending` 显示为“等待中”，而不是“已取消”。

所以本轮要点中的“站点身份保留”已 PASS；“站点状态在所有生命周期阶段正确落地”仍 FAIL。它是局部补丁级问题，不需要重做 Assistant Transport 或引入新的事实模型；但按开发文档的验收标准，不能将最终判定改为通过。建议后续在 UI projection/renderer 之间明确 cancelled/running 的优先级，并补上 running、cancelled 的 snapshot 与 DOM 测试；本轮遵守要求不改源码。

### 3. 本轮按 assistant-ui 官方文档复核的事实

通过 assistant-ui 官方文档 MCP 重新核对：

- [Tool UI](https://www.assistant-ui.com/docs/tools/tool-ui)：renderer 是 render-only 视图，应根据 tool args/result/status 处理 loading、success、error；本项目 renderer 读取 artifact，不执行 Web 请求。
- [Tool Call](https://www.assistant-ui.com/elements/tool-call)：工具调用应保持可见触发器，详情通过 disclosure 展开；当前 `web_search` 使用 `Collapsible`，`web_extract` 保持状态行。
- [Tool Group](https://www.assistant-ui.com/elements/tool-group)：当前连续工具组使用 `ToolGroupRoot variant="ghost"`，符合低噪音折叠表面。
- [Part Grouping](https://www.assistant-ui.com/docs/guides/part-grouping)：`thread.aui.tsx` 使用 `MessagePrimitive.GroupedParts` 的 `groupBy`，从 artifact 的 `presentation.surface` 区分 standalone 与 tool trace；standalone 返回空路径，常规工具进入 `group-tool-trace`。
- [Custom AssistantTransport](https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport)：后端发送状态快照、前端 converter 映射为 assistant-ui 消息；当前 `converter.ts` 把 Web tool 的正文 result 置为 undefined，UI 仍是快照之上的无事实视图。

### 4. 本轮代码路径结论

| 验收项 | 当前事实 | 判定 |
| --- | --- | --- |
| 失败/取消无新 data 时保留站点身份 | dispatcher 返回 `data=None`，projector 不更新 `part.data` | PASS |
| 失败/取消最终工具状态 | projector 写入 `part.status=failed/cancelled`，`result=None` | PASS |
| `web_extract` 逐站 running/cancelled 状态 | running/cancelled 无新 data，站点 data 仍为 pending；renderer 不覆盖 cancelled | FAIL（本轮阻塞） |
| 正文不进入 UI | 后端 projection/snapshot validator、dispatcher result 截断、converter sanitizer、renderer 均不显示正文 | PASS |
| `web_search` renderer | `web-search-results` 专用 renderer，可折叠结果标题/摘要/URL，不重新抓取 | PASS |
| `web_extract` renderer | 仅显示站点和状态，不读取 `content`/`metadata` | PASS，但取消显示受上项影响 |
| Snapshot 校验 | Web data 使用 kind 和字段 allowlist，拒绝 `content`/`metadata`，Web result 不接受正文 | PASS |
| GroupedParts/tool-group | `surface` 驱动 grouping，当前 tool group 显式 `ghost` | PASS |
| quiet 无边框 UI | 当前 Thread 路径使用 ghost；details/diff/terminal 已去外围 card/border | PASS |

### 5. 本轮测试

通过：

```text
Backend:
uv run --no-cache pytest tests/test_web_search_extract_tools.py tests/test_tool_observation_summary.py tests/test_tool_observation_dispatcher.py tests/test_conversation_event_projector.py -q
100 passed in 2.49s

Frontend:
npx.cmd vitest run tests/unit/converter.test.ts tests/unit/tool-part.test.ts
2 files, 14 tests passed
```

另外复核 `git diff --check`：未发现本轮验收文档相关的 whitespace 错误。工作区存在大量既有修改和未跟踪文件，本轮未修改这些源码；此前报告记录的完整后端测试中 `test_context_usage_compute_listener.py` 两项失败仍属于既有失败，不作为本次 UI 改造回归。

### 6. 第二轮最终判定

| 结论 | 判定 |
| --- | --- |
| 上一轮“空 data 覆盖创建站点列表”是否解决 | 是 |
| 失败/取消最终快照是否保留网站身份和工具终态 | 是 |
| Web Extract 逐站状态是否覆盖开发文档要求的全部状态 | 否，running/cancelled 仍退化为 pending 显示 |
| 是否存在结构性改造阻塞 | 否，属于局部 UI projection/renderer 补丁 |
| 本轮实现验收是否通过 | **否** |

以下为首轮验收和历史修复记录，保留用于追踪。

---


> 验收日期：2026-09-08
>
> 验收范围：`read_file`、`write_file`、`patch`、`apply_patch`、`search_files`、`list_directory`、`delete`、`execute_terminal`、`web_search`、`web_extract`。
>
> 明确排除：`delegate_task` 以及所有 CodeGraph 工具。本报告不评价、不修改这些工具。
>
> 本次验收只读取代码、运行测试并写入本报告；没有修改任何源码。

> 以下内容是第一轮验收的历史记录，已被上方“第三轮最终独立验收结论”取代；其中的“否”不代表当前最终判定。

## 历史记录：第一轮实现验收（已被第三轮最终复核取代）

**首次验收暂不通过；修复后需复验。**

核心的 UI 投影、正文隔离、Assistant Transport 快照校验、专用 renderer、`GroupedParts` 分组和 quiet UI 已落地，且成功/部分成功路径验证通过。但存在一个失败生命周期的真实 UI bug：`web_extract` 在 Provider 失败、参数失败或全部页面失败时，最终快照可能丢失创建阶段的站点列表，只显示总体失败状态，不能稳定显示“哪个网站失败”。

该问题是**局部补丁问题，不是结构性改造阻塞**。建议修复后重新运行本报告中的专项测试，再宣布通过。

### 1.1 首次验收后的修复记录

根据上述验收结论完成了局部补丁：

- `apps/backend/app/core/workflows/nodes/helper/tool_observation_dispatcher.py` 新增 `_ui_data`；Web 工具在失败/取消且没有新的 UI data 时发送 `data=None`，不再用空投影覆盖创建阶段的 pending 网站列表。
- `apps/backend/tests/test_tool_observation_dispatcher.py` 增加失败态无新 data 的断言。
- `apps/backend/tests/test_conversation_event_projector.py` 增加失败事件不丢失站点身份的快照断言。
- 修复后 Web/Transport 专项测试为 `99 passed`，但本报告的最终判定仍等待下一轮独立验收。

### 历史记录：官方 assistant-ui 依据

已通过 `assistant-ui-docs` 官方文档 MCP 核对：

- [Tool UI](https://www.assistant-ui.com/docs/tools/tool-ui)：后端工具可以提供 render-only UI；renderer 根据 `args`、`result`、`status` 处理 loading、success、error 状态。
- [Tool Call](https://www.assistant-ui.com/elements/tool-call)：工具调用应有可见触发器，详情使用 disclosure 展开；展开状态可以由组件临时控制。
- [Tool Group](https://www.assistant-ui.com/elements/tool-group)：连续工具调用可以合并为折叠组，官方示例支持 `ToolGroupRoot variant="ghost"`。
- [Part Grouping](https://www.assistant-ui.com/docs/guides/part-grouping)：使用 `MessagePrimitive.GroupedParts`，`groupBy` 根据 part 和上下文返回分组路径；返回空路径可让 part 保持独立，`display: "standalone"` 可使工具脱离普通工具组。
- [Custom AssistantTransport](https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport)：后端传输完整状态快照，前端 converter 将快照转换为 assistant-ui 消息；UI 是状态快照之上的无事实视图。

当前实现使用的是项目已有的自定义 Assistant Transport 和手工 `ToolPart` 路由，没有重新引入客户端工具执行链或旧 SSE，符合上述边界。

### 历史记录：代码事实验收

| 验收项 | 代码事实 | 结论 |
| --- | --- | --- |
| Web UI projection | `apps/backend/app/core/tools/tool_execute/tool_ui_projection.py` 的 `build_web_search_ui_data`、`build_web_extract_ui_data`、`project_tool_ui_data` 使用字段白名单；`web_search.py` 返回 `web-search-results`，`web_extract.py` 返回 `web-extract-status` | PASS |
| `web_extract` 不显示正文 | `build_web_extract_ui_data` 不复制 `content`/`metadata`；`tool_observation_dispatcher.py:_ui_result` 对 Web 工具返回 `None`；`conversation_state_snapshot.py:_validate_web_tool_data` 拒绝正文字段和非空 `result`；`converter.ts:sanitizeWebToolData` 只保留站点状态字段；`WebExtractStatusTool` 只遍历 `sites` | PASS（硬约束满足） |
| 创建到 Transport 的 data 时序 | `model_node.py` 使用 `build_initial_tool_ui_data` 给 `ToolCallCreatedEvent` 写入 pending 站点；`conversation_event_projector.py:_plan_tool_created` 写入 part data；有 data 的状态事件更新 data，无 data 的状态事件保留原 data | PASS（正常有 data 路径） |
| `web_search` renderer | `web-search-tool.tsx` 以无边框摘要行显示查询、结果数和状态，使用 `Collapsible` 展开标题、摘要和已返回 URL，不重新抓取网页 | PASS |
| `web_extract` renderer | `web-extract-status-tool.tsx` 只显示 `site` 和状态图标/文字，无正文展开入口，也不读取 `content` 或 `metadata` | PASS |
| renderer 路由 | `tool-part.tsx:routeToolPart` 先按 `data.kind` 路由 `web-search-results` / `web-extract-status`，未知 kind 仍走 fallback | PASS |
| GroupedParts / tool-group | `thread.aui.tsx:assistantMessageGroupBy` 读取 `artifact.presentation.surface`；非 standalone 工具进入 `group-tool-trace`，standalone 返回空路径；当前组使用 `ToolGroupRoot variant="ghost"` | PASS |
| quiet UI | `details-tool.tsx`、`diff-tool.tsx`、`terminal-tool.tsx` 的当前渲染路径已移除外围 border/card；ToolGroup 当前线程使用 ghost；终端只保留必要的深色内容背景 | PASS |
| 其他范围内工具路由 | 文件、目录、删除、Diff、终端仍通过既有 `kind`/`presentation` 分发，未引入客户端执行逻辑 | PASS |

### 历史记录：阻塞项——失败态丢失 Web Extract 站点身份（已修复）

### 代码链路

1. `model_node.py` 在创建工具调用时，已经根据参数生成站点列表：

   `build_initial_tool_ui_data("web_extract", {"urls": [...]})` 返回 `web-extract-status` 和 `pending` 站点。

2. `tool_error.py:tool_error` 在没有结构化 data 的错误观察中将 `observation.data` 置为 `{}`。

3. `tool_observation_summary.py` 对 Web 工具调用 `project_tool_ui_data`。当前事实是：

   ```text
   project_tool_ui_data("web_extract", {})
   -> { kind: "web-extract-status", provider: "", sites: [] }
   ```

4. `tool_observation_dispatcher.py:dispatch_tool_observations` 始终发送：

   ```python
   data=copy.deepcopy(summary.get("data") or {})
   ```

   因此失败、Provider 不可用、非法参数、全部页面失败等没有结果 data 的调用，会发送一个空站点数组，而不是“不更新创建阶段的 data”。

5. `conversation_event_projector.py:_plan_tool_status` 对 `event.data is not None` 会覆盖现有 part data。最终 `web-extract-status-tool.tsx` 收到 `sites=[]`，只能显示总体状态，无法显示具体失败网站。

这违反开发文档第 5.3、11.3 节的要求：多 URL 以及失败、Provider error、invalid URL、取消状态都应尽量保留站点身份；缺少身份时才退化为总体状态。

### 最小修复建议

不需要重做 Transport 或引入新状态模型。应在“无新的 UI data”时传递 `None`，让 projector 保留创建阶段 data；或者在 projector 处明确把“空的 Web 状态投影”视为不更新。随后补充 Provider 失败、参数失败、全部页面失败和取消的快照断言。

修复后至少应断言：

```text
created: sites=[example.com pending]
failed without new data: sites=[example.com failed-by-overall-tool-status]
```

同时仍必须保持 `result is None`，不能把模型正文放回 Transport。

### 历史记录：测试结果

### 通过

- 后端专项测试：

  ```text
  uv run --no-cache pytest tests/test_web_search_extract_tools.py \
    tests/test_tool_observation_summary.py \
    tests/test_tool_observation_dispatcher.py \
    tests/test_conversation_event_projector.py -q
  98 passed
  ```

- 前端单元测试：`npm.cmd run test:unit`，14 个测试文件、46 个测试通过。
- 前端类型检查与构建：`npm.cmd run build` 通过。
- 前端 ESLint：`npm.cmd run lint` 通过。
- 后端本次相关文件 Ruff：`uv run --no-cache ruff check ...` 通过。
- 后端完整测试在使用工作区临时目录后：166 passed，只有下述 2 个与本次范围无关的失败。

### 与本次改造无关的已有失败

完整后端测试中的两个失败均来自 `tests/test_context_usage_compute_listener.py`：

- 测试仍以 `publish_event` 参数构造 `ContextUsageComputeListener`，而当前构造器不接受该参数。
- 无 `publish_event` 的测试路径要求未初始化的 storage fixture。

首次使用默认 Windows 临时目录时，另有 10 个工具执行测试因 `C:\Users\Administrator\AppData\Local\Temp\pytest-of-Administrator` 权限错误无法 setup；切换到工作区 `--basetemp` 后这些错误消失，证明它们不是本次代码回归。

### 尚未完成的验证

- 没有运行真实桌面进程下的 Playwright/E2E 或人工重连验收。
- 当前新增前端测试覆盖了 converter 的正文隔离和 `ToolPart` 路由，但尚未覆盖两个 renderer 的 loading、empty、partial、provider-error DOM 状态。
- 尚未有一条真实 HTTP Assistant Transport reconnect 测试覆盖失败态站点列表恢复。

这些属于发布前补充验证；其中失败态站点身份问题在修复前不能被 E2E 结果掩盖。

### 历史记录：剩余风险

1. **失败态站点丢失：阻塞，局部补丁可修复。** 见第 4 节。
2. `tool-group.aui.tsx` 仍保留 `outline`/`muted` 变体的 border 样式，且未传 variant 的旧 wrapper 的 `data-variant` 默认值仍是 outline；当前 `Thread` 明确传入 ghost，因此当前改造路径是 quiet，但未来复用旧 wrapper 时可能重新出现边框。建议后续统一默认值和 data attribute，属于小范围样式补丁，不是本次 Transport 结构问题。
3. 外部 URL 使用普通 `<a target="_blank">`，当前代码没有单独的桌面外部浏览器适配验收；需要在 Tauri WebView 中人工确认点击行为。
4. Web provider 只由本机后端访问，前端没有直接网络请求；该本地桌面进程边界未被本次 UI 改造改变。

### 历史记录：第一轮最终判定（已过时）

| 结论 | 判定 |
| --- | --- |
| 方案落点是否基本实现 | 是 |
| `web_extract` 正文是否进入 UI | 否，当前隔离链路有效 |
| `web_extract` 是否所有失败状态都能显示具体网站 | 否，空 data 会覆盖创建阶段站点列表 |
| 是否需要结构性改造 | 否，当前阻塞是局部补丁 |
| 本次实现验收是否通过 | **否** |

完成失败态 data 保留补丁、补齐对应快照/renderer 状态测试，并在桌面进程中完成一次 Web Search 展开、Web Extract 失败态和重连验收后，再进行下一轮独立验收。
