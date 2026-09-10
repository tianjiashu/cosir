# 非委派、非 CodeGraph 工具 UI 改造方案

> 状态：本文是历史方案，已被 2026-09-10 的绿地简化决策取代。当前实现不再使用 Web 专用
> `kind`、projection、snapshot allowlist 或专用 renderer；`web_search` / `web_extract`
> 统一输出通用 `data.entries`，前端复用 `DetailsTool`，正文可见性由通用
> `ToolDisplayHints.show_result` 声明控制。

> 范围：`read_file`、`write_file`、`patch`、`apply_patch`、`search_files`、`list_directory`、`delete`、`execute_terminal`、`web_search`、`web_extract`。
>
> 明确排除：`delegate_task` 以及所有 CodeGraph 工具。本方案只做设计和验收约束，不包含本次代码修改。

## 1. 结论

需要改造，但不需要重做 Assistant Transport，也不需要让前端执行工具。当前最明显的缺口是：

1. `web_search` 和 `web_extract` 没有专用语义化 renderer，都会落入通用 `DetailsTool`。
2. `DetailsTool` 只识别 `data.entries` / `data.files`，而 Web 工具实际使用 `data.web`，因此当前搜索/提取结果不能可靠渲染。
3. `web_extract` 的 `data.web` 当前包含 `content`，与“只显示网站和状态、不显示文档正文”的产品要求冲突。
4. 工具 UI 目前大量使用 card、border、rounded 和背景块，视觉重量偏大；应改为无外框、低对比度、以状态图标和间距表达层级的静默行。
5. 当前 `ToolPart` 已经从后端 tool-call part 的 `artifact` 读取 `presentation`、`data`、`error`，这条适配边界可以保留；改造重点是后端展示数据契约和前端 renderer 路由。

目标形态是：

```text
主消息流
  ├─ 静默工具行：读取、搜索文件、目录、网页提取状态
  ├─ 可展开工具行：网页搜索结果
  ├─ 结果工具行：文件变更、删除、终端
  └─ 未识别工具：保留安静的通用 fallback
```

工具行不再默认绘制卡片边框。只有内容本身需要结构化阅读时，才保留轻量背景、语法色或等宽字体。

## 2. 运行拓扑和事实来源

这是单用户本地桌面 Agent，不按公网 SaaS 设计：

| 部分 | 运行位置 | 职责与数据边界 |
| --- | --- | --- |
| React/Tauri 前端进程 | 桌面端 WebView | 只负责 Assistant UI 渲染、展开/收起、选中态和连接；不执行工具，不保存对话事实 |
| FastAPI 后端进程 | 本机进程 | 负责 Agent Runtime、工具权限、工具执行、观察结果、canonical conversation state 和 Assistant Transport |
| LangGraph / 工具 worker | 后端进程内，必要时为本地子进程 | 执行 workflow 和工具；工具隔离/超时由后端控制 |
| SQLite | 本机磁盘 | 保存对话、消息 part、运行状态、快照等后端事实 |
| 外部 Web provider | 仅由后端访问 | `web_search` / `web_extract` 的网络出口；前端不得直接请求 URL 或 provider |

桌面端启动本地 supervisor，supervisor 负责启动、停止和恢复 FastAPI；前端通过本机 Assistant Transport 连接。后端崩溃后由 supervisor 恢复，前端重新连接并从后端快照重建 UI。工具 UI 的展开状态可以留在 React 临时状态中，不能写回 SQLite，也不能成为下一次工具请求的输入。

## 3. 当前代码事实基线

### 后端

- 工具注册集中在 `apps/backend/app/core/tools/tool_system.py`。
- `ToolObservation.content` 是模型观察通道，`ToolObservation.data` 是客户端可渲染数据，`internal_data` 不应进入前端。
- `ToolCallStatusChangedEvent.data` 通过 `ConversationEventProjector` 投影到 Assistant Transport 的 tool-call part；前端 converter 再把它放到 tool-call part 的 `artifact`。
- `apps/backend/app/core/tools/tool_handler/web_search.py` 当前将搜索结果放在 `data={"web": web_results}`，结果项包含 `title`、`url`、`description`、`position`、`provider`。
- `apps/backend/app/core/tools/tool_handler/web_extract.py` 当前将提取结果放在 `data={"web": results}`；结果项包含 `url`、`title`、`content`、`provider`、`metadata`、`truncated`、`error`。
- `web_search` 和 `web_extract` 的 `ToolDisplayHints.expand_layout` 当前都是 `list`。

### 前端

- `apps/desktop/components/assistant-ui/tools/tool-part.tsx` 是当前工具 renderer 的路由入口，目前只对 delete、diff、terminal、details 做专门分流。
- `apps/desktop/components/assistant-ui/tools/details-tool.tsx` 的列表分支只读取 `entries` / `files`，不能正确消费 `web`；其列表分支还不能完整呈现 web 错误。
- `apps/desktop/components/assistant-ui/tools/diff-tool.tsx` 使用 `react-diff-view`；`terminal-tool.tsx` 提供终端输出；`delete-tool.tsx` 和 `unknown-tool.tsx` 已经接近行式 UI。
- `apps/desktop/components/assistant-ui/elements/thread.aui.tsx` 手工渲染 tool-call part，当前没有使用 assistant-ui toolkit 注册机制。
- `apps/desktop/lib/assistant/converter.ts` 已经把后端 `status`、`presentation`、`data`、`error` 映射到前端 artifact。不要让 renderer 重新推导后端领域状态。

## 4. assistant-ui 官方建议如何落到本项目

本方案依据以下官方文档：

- [Tool UI](https://www.assistant-ui.com/docs/tools/tool-ui)：后端工具可以只提供 render-only UI；renderer 接收 `args`、`result`、`status`，负责 loading、success、error 三种显示。
- [Tool Call](https://www.assistant-ui.com/elements/tool-call)：工具调用应有始终可见的触发器，详细内容通过 disclosure 展开；静态 UI 也可以由 `open` / `onOpenChange` 控制。
- [Tool Group](https://www.assistant-ui.com/elements/tool-group)：连续的常规工具调用可以收进折叠组，并支持 ghost 风格，减少工具噪音。
- [Tool Timeline](https://www.assistant-ui.com/elements/tool-timeline)：整段工具活动可以抽象成单条可收起的时间线；本项目应先沿用现有 Thread 结构，再逐步接入该语义。
- [Part Grouping](https://www.assistant-ui.com/docs/guides/part-grouping)：使用 `MessagePrimitive.GroupedParts` 组织相邻 part；需要始终可见的工具可使用 standalone 语义脱离折叠组。
- [Assistant Transport](https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport)：Transport 传输后端状态快照，converter 负责协议映射；前端 UI state 不是后端事实源。

对本项目的具体原则：

1. 继续使用 Assistant Transport + converter，不重新引入旧 SSE、前端 event store 或客户端 tool execution。
2. 每个需要特殊呈现的 backend tool 使用独立 renderer；不要继续把 Web 工具塞进通用文件列表 renderer。
3. renderer 只读 `artifact.data`、`artifact.presentation`、`status` 和 `error`，不解析模型文本，不从 `args` 猜测执行结果。
4. 使用 tool-call 的 disclosure 语义，不默认展开长结果；执行中显示简短进行中状态，结束后保留摘要。
5. `display: "standalone"` 只用于用户需要直接看到的结果；常规观察型工具进入安静的 tool group 或 flat rows。

## 5. 统一展示数据契约

### 5.1 现有 kind 的处理策略

| 工具 | 当前/目标 `data.kind` | renderer 策略 |
| --- | --- | --- |
| `read_file` | `read-file-meta` | 安静单行，仅显示文件和状态；不显示文件正文 |
| `write_file`、`patch`、`apply_patch` | `file-changes` | 无外框 diff；保留变更统计和语法 diff 阅读能力 |
| `search_files` | `file-list` | 安静的可折叠文件列表；无外框、无 card |
| `list_directory` | `directory-list` | 安静的可折叠目录列表；无外框、无 card |
| `delete` | `delete-result` | 安静单行，显示目标和成功/失败 |
| `execute_terminal` | `terminal-result` | 无外框等宽输出；保留深色背景或低对比度背景以维持可读性 |
| `web_search` | 新增 `web-search-results` | 专用搜索 renderer；默认摘要行，可展开结果列表 |
| `web_extract` | 新增 `web-extract-status` | 专用状态 renderer；永远不显示正文，不提供正文展开入口 |

### 5.2 `web_search` 契约

建议的 `ToolObservation.data`：

```json
{
  "kind": "web-search-results",
  "query": "用户查询",
  "provider": "provider-name",
  "results": [
    {
      "title": "网页标题",
      "url": "https://example.com/page",
      "description": "搜索摘要",
      "position": 1
    }
  ]
}
```

前端首行显示“搜索：查询 · N 条结果 · provider”，展开后显示标题、域名/URL 和摘要。点击链接属于明确的用户操作；前端仍不重新抓取数据，只打开后端已经返回的 URL。

### 5.3 `web_extract` 契约：UI 只保留网站和状态

`content` 必须从客户端展示数据中移除。建议的 `ToolObservation.data`：

```json
{
  "kind": "web-extract-status",
  "provider": "provider-name",
  "sites": [
    {
      "site": "example.com",
      "url": "https://example.com/page",
      "status": "success"
    },
    {
      "site": "another.example",
      "url": "https://another.example/page",
      "status": "failed",
      "error_code": "fetch_failed"
    }
  ]
}
```

约束：

- `sites[*]` 只允许网站身份和状态字段；禁止 `content`、`metadata`、原始响应、正文摘要进入 `data`。
- 完整正文仍可留在 `ToolObservation.content`，供模型上下文使用；这不是 UI 数据，也不应被 converter 放入可见 renderer 的 data。
- `status` 至少区分 `pending`、`running`、`success`、`failed`、`truncated`；工具总体状态由 tool-call part 的 `status` 表达。
- 多 URL 调用必须逐站显示状态，不能只显示一个笼统的“提取完成”。
- URL 可以作为可访问性或打开链接的 identity，但 UI 默认显示 host/site，不显示正文，也不把长 URL 作为主标题。

后端实现时必须将“模型观察投影”和“UI 展示投影”拆开：复用同一次抓取结果，但分别构造 `content` 和 status-only `data`。这里有一条硬边界：`ToolObservation.content` 可以继续用于模型上下文，但不得再无条件映射为面向 Transport 的 `ToolCallStatusChangedEvent.result`。当前 `dispatcher → event.result → snapshot.result → converter` 是正文进入 UI state 的泄漏链，必须在 dispatcher 处截断。

具体约束如下：

- `ToolCallStatusChangedEvent.result` 的语义固定为 UI-safe result；对 `web_extract` 设为 `None` 或 status-only summary，不得放入模型正文。
- 模型正文只通过 observe 节点既有的模型上下文写回路径进入 Runtime context；不新增一个会被 Projector 投影的正文事件字段。
- `ConversationEventProjector` 只投影 `data` 和 UI-safe `result`；snapshot validator 必须拒绝 `data` 中的 `content`、`metadata`、raw response 等字段。
- 前端 converter 不承担脱敏责任，只消费已经经过后端 allowlist 的 `data` 和 `result`。前端 renderer 即使收到意外字段，也必须不渲染正文。

为支持执行前的 pending/running 状态，明确给 `ToolCallCreatedEvent` 增加可选的 `data`，并由后端 UI projection 根据已校验的 args 生成初始站点列表。之后的 running/completed/failed/cancelled 状态事件必须复用同一 `kind`，仅更新站点状态。该数据构造是后端纯展示投影，不得改变 Runtime context、模型消息或工具事实。

## 6. 前端组件改造方案

### 6.1 新增专用 renderer

目录：`apps/desktop/components/assistant-ui/tools/`

- `web-search-tool.tsx`
  - 输入：当前 tool-call 的 `status`、`artifact.data`、`error`。
  - 默认：一行搜索摘要，显示搜索图标、查询、结果数量和状态。
  - 展开：结果列表，标题优先，域名和摘要为次级信息。
  - 空结果、provider 错误、取消和失败均由 renderer 显式处理。
- `web-extract-status-tool.tsx`
  - 输入：`web-extract-status` 数据和 tool-call 状态。
  - 默认和完成态：按站点显示网站名称 + 状态图标/文字。
  - 不渲染 `content`，不提供“查看正文”折叠内容。
  - 多站点使用多行紧凑布局；单站点保持单行。
- `web-tool-row.tsx`（可选共享组件）
  - 只抽取站点、状态、域名、结果数量等视觉原语。
  - 不放 provider 请求、网络访问或数据推导逻辑。

### 6.2 调整路由

在 `tool-part.tsx` 中按稳定的 `data.kind` 优先路由：

```text
web-search-results   → WebSearchTool
web-extract-status   → WebExtractStatusTool
file-changes         → DiffTool
terminal-result      → TerminalTool
delete-result        → DeleteTool
directory-list/file-list/read-file-meta → DetailsTool
其他                 → UnknownTool
```

`web_search` 不再依赖 `expand_layout === "list"` 进入 `DetailsTool`；`web_extract` 也不再复用通用 list fallback。未知 kind 仍保留 fallback，以便后端新增工具时不导致 Thread 崩溃。

### 6.3 分组和 standalone

以 `ToolDisplayHints.surface` 作为后端声明的展示意图，并由前端真正消费：

- `trace`：`read_file`、`search_files`、`list_directory`、`web_extract` 状态行。相邻调用进入 assistant-ui 的 `ToolGroup` 或 flat grouped rows，默认收起。
- `standalone`：`web_search`、文件变更 diff、终端输出、删除结果。触发器可见，结果按需要展开。

如果当前 `@assistant-ui/react` 版本的 `GroupedParts` 无法直接访问 converter 放入 artifact 的 `surface`，先保留现有手工 `ToolPart` 路由，只实现 flat quiet rows；随后补一个 UI 边界内的 grouping adapter。不要把 assistant-ui 类型反向引入后端，也不要通过工具名称在 Thread 中硬编码分组规则。

## 7. “安静一些、去掉边框”的视觉规范

所有工具 renderer 统一采用以下规则：

- 工具根节点移除 `border`、外层 `rounded-*` 和 card 阴影。
- 触发器使用普通行布局：图标、动作词、目标/摘要、状态；低对比度文字，hover 只改变背景或文字色。
- 结构层级用垂直间距、缩进、字体层级和状态色表达，不用外框嵌套。
- 列表不使用外框；结果之间优先使用间距。若长列表确实需要分隔，使用极弱的内部 divider，不加包围边框。
- 进行中只显示 spinner/进行中词；成功显示 check；失败显示错误色和短错误，不把完整异常堆栈塞入主消息流。
- `web_extract` 的失败状态只显示站点和短状态，不显示抓取正文或原始异常内容。
- diff 保留增删颜色和等宽字体，但去掉外层 border/card；终端保留必要的背景和滚动，不用边框包围。
- 默认收起长列表、diff 和终端；不要因每个 tool-call 都产生大块垂直占位。
- 不使用 `result` 文本作为通用 `<pre>` 回退来渲染 Web 数据；fallback 只用于未知工具诊断。

## 8. 后端改造分解

1. 在 web handler 内增加稳定的 UI projection：搜索生成 `web-search-results`，提取生成 `web-extract-status`。
2. 保证提取正文只进入模型 observation content，不进入 UI data；对单 URL、多 URL、部分成功、失败、取消、截断分别生成状态。
3. 补齐 ToolCall 创建/状态事件的数据时序，使前端在 pending、running 阶段也能显示目标网站。
4. 修正 `ToolDisplayHints`：明确 web search 是否 standalone，web extract status 是否 trace；建议采用第 6.3 节的分组策略。
5. 不修改工具执行入口、权限门禁、预算、Runtime context 或 SQLite 事实模型；展示投影只在事件/Transport 适配边界传递。

## 9. 前端改造分解

1. 新增两个 Web renderer 和必要的共享状态原语。
2. 调整 `tool-part.tsx` 路由，先看 `data.kind`，再选择 renderer。
3. 收敛 `DetailsTool` 的职责，只负责文件/目录元数据列表，并补齐未知/失败态。
4. 移除已有工具 renderer 的外层 border/card 样式，保持 diff、终端内容的可读性。
5. 按官方 disclosure/grouping 语义实现默认收起和连续工具分组。
6. 不在 React state、localStorage 或浏览器端保存工具结果事实；展开态只是临时 UI 状态。

## 10. 验证与验收

### 后端契约测试

- `web_search` 成功、空结果、provider 失败：`data.kind` 和字段稳定。
- `web_extract` 单 URL、多 URL、部分成功、全失败、取消、截断：`data` 中不存在 `content` / `metadata` / 原始响应。
- 完整正文仍存在于模型观察通道，并能正常进入后续模型上下文。
- Transport snapshot / reconnect 后，tool-call 状态、站点列表和错误状态一致。
- 现有文件、终端、删除、diff 工具契约不回归。

### 前端单元测试

- `ToolPart` 能按每种 `kind` 路由到正确 renderer。
- Web search 能正确显示 loading、success、empty、partial/error。
- Web extract 只显示网站和状态；即使传入恶意或意外的 `content` 字段，renderer 也不渲染。
- 多站点顺序稳定，站点状态与后端 snapshot 一致。
- 未知 kind 仍由 fallback 安全渲染。
- 工具根节点不产生外层 border/card class；diff/terminal 仅保留内容可读性所需样式。

### E2E / 人工验收

- 连续执行 `read_file`、`search_files`、`list_directory`、`web_extract` 时，主消息流保持低噪音，可整体收起。
- 执行 `web_search` 时，主行安静可见，点击后只展开搜索结果摘要和链接。
- 执行 `web_extract` 时，任何状态下都只看到网站和状态，不能看到文档正文。
- 后端重启/前端重连后，已完成工具仍能恢复；展开状态丢失是允许的，工具事实丢失不允许。
- 前端 Network/Transport payload 中，`web_extract` 的 UI data 不包含正文。

## 11. 针对首轮验收 FAIL 的修订闭环

本节是对首轮独立验收文档中 F1–F7、R1–R5 的直接修订，不是“以后再考虑”的建议；实现完成前，以下条件均属于阻断项。

### 11.1 建立后端字段级 allowlist，而不是只做预算截断

新增 UI projection 层，位置建议为 `apps/backend/app/core/tools/tool_execute/` 下的独立模块，例如 `tool_observation_ui_projection.py`。它在 `ToolObservationSummary` 和 `tool_observation_dispatcher` 之前运行，职责是：

1. 根据工具定义和已校验输入构造 `web-search-results` / `web-extract-status`。
2. 对 `web_search` 只允许 `query`、`provider`、`results`，结果项只允许 `title`、`url`、`description`、`position`。
3. 对 `web_extract` 只允许 `provider`、`sites`，站点项只允许 `site`、`url`、`status`、`error_code`、`truncated` 和受长度限制的短错误信息。
4. 明确丢弃 `content`、`metadata`、raw response、provider 私有 payload；丢弃动作写结构化诊断日志，但不得记录正文。
5. 先做字段投影，再做 `DisplayDataBudget`；预算组件只负责大小，不负责安全过滤。

`ToolObservation.data` 的 Python 类型应从宽泛 dict 收敛为后端 discriminated union 或等价的 Pydantic allowlist。`ConversationStateSnapshot` 的 tool part 校验也要复用同一 allowlist，形成第二道边界；不能因为当前 `DetailsTool` 不主动显示正文，就认为数据安全。

### 11.2 明确模型结果与 UI 结果的事件契约

当前 `tool_observation_dispatcher.py` 将 `summary.content` 作为 completed event 的 `result` 是本轮验收确认的直接风险点。改造后的契约必须写成：

```text
ToolObservation.content       → observe / Runtime context（模型通道）
ToolObservation.data          → UI projection → event.data → snapshot.data
UI-safe display result        → event.result → snapshot.result（可选）
model content                 -X→ event.result / snapshot.result / Transport
```

对 `web_extract`，renderer 只依赖 `data.kind = web-extract-status`，因此 completed event 的 `result` 直接为 `None` 最安全。若其他工具确实需要 result 文本，也必须经过明确的 UI-safe result schema；不能把所有 observation content 自动当作 UI result。

### 11.3 覆盖所有 Web 状态和事件时序

后端需要为以下阶段定义相同的 status-only shape：

- created/pending：从 args 生成站点身份，状态为 `pending`；
- running：站点状态为 `running`，不填充正文；
- completed：逐站 `success` / `truncated`，不填充正文；
- 部分成功：成功站点和失败站点同时存在；
- 全失败、provider error、invalid URL、取消：仍保留可安全显示的站点和状态，缺少站点身份时只显示总体状态；
- 空搜索结果：`web-search-results` 保留空数组和明确 empty 状态，不回退为通用 `<pre>`。

`ToolCallCreatedEvent.data`、`ToolCallStatusChangedEvent.data`、snapshot part.data 和 converter artifact.data 必须在全链路保持同一 kind。后端应增加 Transport 序列化测试，断言任何阶段的 `web_extract` payload 都不含正文禁止字段。

### 11.4 把 assistant-ui 的分组策略落成明确配置

官方建议只提供原则，仓库必须提供可执行映射：

- `surface=trace` 的连续调用进入 `MessagePrimitive.GroupedParts` 的工具组，默认收起；本项目首批包括 `read_file`、`search_files`、`list_directory`、`web_extract`。
- `surface=standalone` 的工具不被 trace 组吞入；本项目首批包括 `web_search`、`file-changes`、`terminal-result`、`delete-result`。
- `ToolGroup` 使用 `ghost` 或等价无外框变体，禁止默认 `outline` card 样式。
- 分组函数读取 converter 保留的 `presentation.surface`，不在 Thread 中硬编码工具名；如果当前 assistant-ui 版本的 GroupedParts 类型无法访问 artifact，应在 UI 边界增加类型安全 adapter，并以 surface 为唯一输入。
- 用户打开或收起后的状态只存组件临时 state；snapshot 更新不得重置已由用户选择的 open state。

这使“官方 grouping 建议”和“去掉边框”同时成为可验收行为，而不是只存在于设计文字中。

### 11.5 修正测试基线和失败归因

Web 后端测试必须使用与生产选择逻辑一致的 fake provider 注入入口，明确注册 fake backend 或显式传入 provider；不得让测试因默认选择 firecrawl 而绕过 fake provider。验收门禁为：

- Web tool、observation summary、dispatcher、projector、snapshot contract 测试全部通过；
- 不允许以“fixture/config 问题”带着失败进入验收；若 provider fixture 仍失败，判定为 FAIL；
- 前端补充 converter、tool-part、两个 Web renderer 的状态和禁止字段测试；
- 至少有一条 Transport payload 级断言，证明 `web_extract` 正文没有进入 `event.result`、snapshot.result、snapshot.data 或 converter artifact。

### 11.6 把“方案验收”和“实现验收”分开

本文件是实施方案，不把当前代码尚未实现误写成已通过。子 Agent 对本文件的方案验收应检查：每个首轮 FAIL 是否有确定的代码落点、数据不变量、测试断言和失败门禁；实现完成后的工程验收才检查代码和测试是否达到这些条件。两者都必须 PASS，才能宣布本次 UI 改造完成。

## 12. 实施顺序和风险控制

建议按以下顺序落地：

1. 先实现后端 allowlist、模型/UI 通道分离和 Web 状态事件时序；同时修复 fake provider 测试基线。
2. 再实现两个专用 renderer、路由和 converter/Transport 安全测试。
3. 然后统一移除工具外框，并落地 ghost tool group / standalone 分组。
4. 最后运行前端 unit、后端 pytest、Transport payload/reconnect 和 Playwright E2E；任何正文泄漏或 Web 测试失败都不能通过。

不建议本次引入新的 Web UI 依赖：项目已经有 `@assistant-ui/react`、现有 Collapsible、`react-diff-view` 和 streamdown；应优先复用它们。最大的风险是把“给模型的正文”和“给 UI 的状态”混成一个 payload，或在前端用通用文本重新推导状态；这两种做法都会破坏当前后端 canonical state / Assistant Transport 边界。
