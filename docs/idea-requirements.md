# 想法需求文档

## 当前主题

- 将客户端从“首页直接输入创建单轮 task”升级为“按 workspace 创建 task，task 内支持多轮连续对话”的产品形态。
- 将后端从“task 跑一次”升级为“task 作为长期对话容器，turn 作为一次完整运行单元”的运行模型。
- 将对话显示从“当前轮回答视图”升级为“连续 timeline + 结构化 runtime event + 多 renderer 扩展架构”。

## 当前结论

- `workspace` 是本地项目，也是后续沙箱边界。
- 一个 `workspace` 下可以有多个 `task`。
- 一个 `task` 是连续对话容器，不是单轮执行记录。
- 一个 `task` 下可以有多个 `turn`。
- `turnId` 的定义是：一次用户发起输入后，到本轮 assistant 输出、tool 调用、状态收束为止的完整运行单元。
- 后端去掉 `session` 语义，核心边界收敛为：
  - `workspaceId`
  - `taskId`
  - `turnId`
- `stream` 协议围绕 `turnId`，而不是 `taskId`。
- `task` 负责聚合历史 timeline，`turn` 负责单次实时运行流。
- 点击“新建任务”后，应进入专门的新建任务页面。
- 新建任务页面至少包含两个动作：
  - 选择 workspace
  - 输入首条消息
- 发送首条消息后，应同时完成：
  - 创建 task
  - 进入该 task 的对话页
  - 创建首个 turn
- 左侧栏显示历史 workspace 列表，支持折叠 / 展开。
- 左侧每个 workspace 下显示多个 task。
- 点击左侧历史 task 时，应进入该 task 并继续多轮对话。
- 删除 workspace 时，应同时删除该 workspace 以及其下所有 task 记录。
- 左侧临时 task 条目标题默认取首条用户消息摘要。
- 客户端必须支持回显，而且不只是消息区回显：
  - 主对话区要回显用户消息
  - 左侧栏要回显新建出来的 task 条目
- 对话页必须是同一个 task 下的连续 timeline；第二轮对话不应清空第一轮历史。
- tool、status、approval 等过程信息必须按真实顺序显示。
- 第一版内部架构要支持多 renderer，但 UI 先只实现：
  - `markdown`
  - `tool`
  - `status`

## 已确认想法

### 1. 实体边界

- `workspace`：本地项目、任务归属容器、后续沙箱边界。
- `task`：长期对话容器。
- `turn`：一次完整对话运行。
- `message`：用户可读内容单元。
- `event`：运行时事实记录。
- `timeline`：`message + event` 的显示投影。

### 2. 页面流

- 首页空状态不应继续承担“输入即发任务”的职责。
- 用户点击“新建任务”后，应进入专门的新建任务界面。
- 新建任务界面需要先确定 workspace，再输入首条消息。
- 发送首条消息后，客户端应直接进入该 task 的对话页，而不是停留在创建页等待。

### 3. 左侧栏

- 左侧栏展示历史 workspace 列表。
- workspace 支持折叠 / 展开。
- workspace 下显示多个 task。
- 左侧栏需要和主区共享同一套 task 创建状态。
- 左侧临时 task 标题默认取首条用户消息摘要。
- 点击历史 task 时，应恢复并继续该 task。
- 删除 workspace 时，连同其下所有 task 记录一起删除。

### 4. 多轮对话

- 同一个 task 下发送第二轮消息时，不能创建新的 task。
- 同一个 task 下的历史消息、工具事件、状态事件都应连续保留。
- 每次用户发送一轮新输入，都应产生一个新的 `turnId`。
- 每个 `turn` 建立独立 stream。
- 同一个 `turnId` 不能被重复启动；重连只允许接入已有流或回放已有事件。

### 5. 对话显示模型

- 对话页长期模型应是 timeline，而不是“当前轮回答视图”。
- tool / status / approval 不应塞在回答块后面，而应进入 timeline。
- 第一版约束：
  - 一个 `turn` 只有一条 user message。
  - 一个 `turn` 最终最多归并出一条 assistant final message。
  - assistant streaming delta 属于 event，不单独作为 message 落地。
  - tool / status / approval 先作为 event 投影成 timeline item。

### 6. turn 与 stream 协议

- `POST /tasks/{task_id}/turns` 只负责创建 turn，初始状态为 `pending`。
- `GET /turns/{turn_id}/stream` 的长期语义是：
  - 如果 turn 是 `pending`，则启动运行并开始推流。
  - 如果 turn 已经在 `running`，则接入当前流。
  - 如果 turn 已经 `completed / failed / cancelled`，则回放已有事件并结束。
- turn 状态机至少应包含：
  - `pending`
  - `running`
  - `completed`
  - `failed`
  - `cancelled`

## 用户原话

### 关于连续对话

- “目前coding-agent 的对话页面有很多问题，首先我发起第一轮对话后，模型回复。然后再发起一轮对话，对话页就会清空。而不是连续。还有就是tool等event，不是按照event顺序排列的。”

### 关于显示架构

- “我希望对话不仅是markdown渲染，后续还会支持其他显示等等。应该怎么实现客户端的显示？”
- “第一版先做到‘内部架构支持多 renderer，但 UI 暂时只实现 markdown/tool/status’，同时后端event需要改造，目前event比较单一，缺少信息。”

### 关于 event

- “event是有限的吧，为什么需要event表？”
- “每个type event结构不一样吧”

### 关于客户端交互

- “客户端，应该点击新建任务，进入新建任务界面。选择工作区，输入文字，发送就创建了新任务。”
- “一个工作区可以有多个任务，一个任务可以多轮对话。”
- “需要客户端支持回显。”
- “左侧栏也需要回显。”
- “类似这种”

### 关于当前抽象

- “workspace 是本地项目，后续会基于workspace做沙箱。”
- “run的流协议围绕runId（改名为turnId吧）。”
- “turnId 一次用户发起输入后，到本轮 assistant 输出、tool 调用、状态收束为止的完整运行单元。”
- “你对对 message / event / timeline 边界的建议我认可。”
- “stream 改成围绕 turnId”
- “还是按照长期看吧，POST /tasks/{task_id}/turns 只负责创建 turn，初始状态为 pending；GET /turns/{turn_id}/stream …”

## 默认假设

- `workspace -> task -> turn` 是核心领域层级。
- `message` 与 `event` 并行存在，`timeline` 负责把两者投影成最终显示结果。
- 首条发送后的用户消息回显不应等待 `POST /tasks` 和 SSE 完整完成后才出现。
- 左侧栏 sidebar 应与主区共享同一份 task 创建临时态。
- event type 是有限枚举，但 event record 是不断追加的历史事实，需要持久化。
- `sequence` 应由后端统一分配，建议按 task 维度递增，用于稳定排序和断线恢复。
- SSE 只负责实时推送，不应作为完整历史恢复的唯一来源。
- 前端应先拉取 task 历史 events / timeline，再接入 turn stream append 新事件。
- event 表不需要为每个 event type 展开所有字段；公共字段结构化，类型专属字段放入 payload，并由 schema 校验。

## 候选建议

### 1. 客户端交互

- 客户端页面流拆成三种状态：
  - workspace 空状态页
  - 新建任务页（或抽屉 / modal）
  - task 对话页
- 左侧导航树按 `workspace -> tasks` 分层展示。

### 2. 回显策略

- 回显拆成三层：
  - optimistic user echo
  - streaming assistant echo
  - optimistic task echo
- optimistic task echo 的方式是：
  - 先在左侧栏插入一个临时 task 条目
  - 等后端返回正式 `task_id` 后替换

### 3. 显示层架构

- 前端显示层拆成三层：
  - `TimelineStore`
  - `EventNormalizer`
  - `RendererRegistry`
- 客户端数据模型从 `messages + markdown string` 升级为 `TimelineItem + ContentPart`。

### 4. 后端 event 模型

- 后端采用轻量 event log 机制，而不是引入重型 event sourcing 框架。
- 后端新增统一 `RuntimeEvent` 协议，候选核心字段包括：
  - `id`
  - `task_id`
  - `turn_id`
  - `message_id`
  - `tool_call_id`
  - `sequence`
  - `created_at`
  - `type`
  - `role`
  - `title`
  - `summary`
  - `content_parts`
  - `payload`
- 后端新增 `runtime_events` 表，保存 task 运行过程中的每一条事件记录。
- 后端新增统一的 `EventService.emit()` 收口事件写入、校验、排序、落库和 SSE 推送。

### 5. 第一版事件类型候选

- `user.message.created`
- `assistant.message.started`
- `assistant.message.delta`
- `assistant.message.completed`
- `tool.call.started`
- `tool.call.completed`
- `tool.call.failed`
- `turn.status.changed`
- `turn.error`

### 6. `POST /tasks/{task_id}/turns` 请求/响应候选

当前建议，但尚未被用户明确确认为正式协议：

- 请求体候选：
  - `input_text: string`
  - `client_turn_id?: string`
  - `metadata?: Record<string, unknown>`
- 返回体候选：
  - `turn_id: string`
  - `task_id: string`
  - `status: "pending"`
  - `input_text: string`
  - `created_at: string`
  - `updated_at: string`

## 开放问题

### 高优先级

- 新建任务界面是独立页面、抽屉还是 modal，尚未确认。
- workspace 的来源尚未确认：
  - 本地目录选择器
  - 预先登记的工作区列表
  - 项目列表投影
- `POST /workspaces/{workspace_id}/tasks` 与 `POST /tasks/{task_id}/turns` 的首轮创建链路，是否要保持完全对称，尚未确认。

### 中优先级

- 现有后端是否需要补充 `workspace_id`、`turn_id`、`message_id` 等实体边界，尚未确认。
- 客户端本地回显的临时消息 ID、task 临时 ID、失败回滚和重试策略尚未确认。
- 左侧栏临时 task 的排序位置、失败态样式和正式 `task_id` 替换时机尚未确认。
- task 历史 timeline 加载与当前 `turnId` stream 的拼接细节尚未确认。
- 现有 durable run 如何映射到 turn 语义，尚未确认。

### 低优先级

- 前端 normalizer 是否只在客户端实现，还是后端也提供 timeline projection API，待权衡。
- tool event 的输入、输出、错误、耗时、文件引用等字段需要进一步定义。
- checkpoint、approval、human input、subagent event 是否进入第一版协议，待后续阶段确认。
- task 标题摘要的截断规则尚未确认。

## 风险与冲突

### 已确认约束下的实现偏移风险

- 如果实现过程中重新把 `task` 当成单轮执行记录，而不是长期对话容器，会直接破坏多轮对话模型。
- 如果实现过程中让首页输入区继续同时承担“新建任务”和“继续追问”两种职责，会与当前已确认页面流冲突，导致 UI 重新产生歧义。
- 如果实现过程中不做用户消息本地回显，发送后的空白等待期会让用户误判客户端未响应。
- 如果实现过程中不做左侧栏 task 级回显，用户会看到主区已经进入新 task，但 sidebar 里没有对应条目，容易误判为创建失败。
- 如果删除 workspace 被实现成“只删展示不删数据”，会与当前已确认的级联删除语义冲突，导致历史 task 残留和用户心智不一致。
- 如果同一个 `turnId` 能被重复启动，重连或重复点击会造成重复运行，破坏当前确认的 stream 协议约束。

### 仍然存在的架构退化风险

- 如果只修前端 state，不改后端 turn / event 模型，刷新恢复、第二轮连续对话和 tool 顺序问题还会反复出现。
- 如果 event payload 没有 schema 校验，`payload_json` 很容易退化成无边界字段垃圾桶。
- 如果 CRUD 层决定业务 event 语义，会导致分层混乱；event 语义应由 runtime / workflow / tool execution 服务层负责。
- 如果前端直接渲染 raw event，后端内部实现变化会直接冲击 UI；需要 normalizer 做稳定投影。

## 代码调研记录

### 2026-07-17 对话页与 event 链路盘点

- 前端 `ChatPanel` 当前不是 timeline 渲染模型：
  - 用户消息只来自 `taskStore.activeTask.input_text`，因此天然只能显示当前 active task 的一条用户输入。
  - Agent 回复由所有 `model_output_delta` 事件拼接成一个 `AgentMessage`。
  - tool / approval / observation 事件在 AgentMessage 后面统一过滤渲染，无法按真实 event 顺序插入到文本中间。
  - 终态 status 只看 latest event。
- 第二轮对话清空的直接原因：
  - `InputBar` 发送时调用 `useTask.createTask(text)`。
  - `useTask.createTask()` 每次都会 `POST /tasks` 创建新 task。
  - 创建后调用 `setActiveTask(new_task_id)` 和 `clearEvents()`。
  - 因此用户以为是在同一对话继续，系统实际创建了新 task 并清空当前 eventStore。
- 当前后端还没有围绕 `turnId` 重构协议：
  - 现有入口是 `POST /tasks` 创建任务。
  - 当前 `GET /tasks/{task_id}/stream` 同时承担 SSE 和触发任务运行。
  - `GET /tasks/{task_id}/events` 可以读取历史事件。
  - 没有明确的 `POST /tasks/{task_id}/turns` + `GET /turns/{turn_id}/stream` 协议。
- 后端 `run_task` 当前只允许运行 pending task：
  - 如果 task 不是 `pending`，会回放已有 events 并 return。
  - 这意味着 completed task 当前无法追加新 turn 并再次运行。
- 后端已经存在早期 event 表和 RuntimeEvent：
  - `EventModel` 表名为 `events`
  - 字段只有 `event_id`、`task_id`、`event_type`、`payload_json`、`created_at`
  - `RuntimeEvent` 字段只有 `event_type`、`task_id`、`payload`、`event_id`、`created_at`
  - 当前缺少 `sequence`、`turn_id`、`message_id`、`tool_call_id`、`parent_event_id`、`content_parts` 等 timeline/恢复关键字段
- 当前事件排序靠 `created_at + event_id`：
  - 查询函数 `_events_for_task()` 使用 `order_by(created_at, event_id)`
  - 这不如后端统一分配的 task 维度 `sequence` 稳定
- workflow 事件 payload 已经包含部分工具关联信息：
  - `tool_call_requested / started / finished` payload 中已有 `tool_call_id`、`tool_group_id`、`tool_call_index`
  - 但这些字段埋在 payload 里，前端无法作为统一关联字段稳定消费
- tool 事件的“真实时间”还不够准确：
  - 批量工具调用路径中，`operations.execute_tools()` 先执行工具，再统一发 `tool_call_requested`、`tool_call_started`、`tool_call_finished`
  - 因此当前 `tool_call_started` 更像“执行结果回放事件”，不是工具真实开始前的实时事件
  - 如果后续要像 Codex 一样实时展示工具运行，需要把工具 started/completed 事件下沉到 ToolRuntime 执行前后
- 现有 Trace 系统与 RuntimeEvent 是两条链路：
  - Trace 更偏诊断 ledger、日志和 span
  - RuntimeEvent 更偏 UI/runtime timeline
  - 不建议直接用 Trace 替代对话 timeline，但可以复用其 append/sequence/trace 设计经验

## 被推翻或替换的想法

- 不采用“普通 messages 列表 + markdown 渲染”作为对话页长期模型。
- 不建议第一版引入重型 event sourcing 框架。
- 不建议只通过前端保留旧消息来修复第二轮对话清空问题。
- 不建议继续把首页输入框同时当成“新建任务入口”和“继续对话入口”。
- 不再保留后端 `session` 作为长期领域边界。
- 不再把 `taskId` 作为实时 stream 的直接边界。

## 后续可整理方向

- 整理正式技术方案：`docs/chat-timeline-event-architecture.md`
- 梳理 `workspace -> task -> turn -> message / event / timeline` 的数据模型与前后端契约
- 梳理“新建任务页 -> task 对话页 -> 多轮继续对话”的客户端状态流
- 设计前端 `TimelineStore`、`EventNormalizer`、`RendererRegistry` 的 TypeScript 类型
- 设计后端 `RuntimeEvent` schema 与 SQLite 表结构
- 明确 `POST /tasks/{task_id}/turns` 与 `GET /turns/{turn_id}/stream` 的正式请求 / 响应 / SSE 协议
- 盘点现有后端 event、SSE、task/run、tool execution 代码

## 第一版验收方向

- 点击“新建任务”后可以进入专门的新建任务界面。
- 选择 workspace 并发送首条消息后，会创建新 task 并立即进入该 task 对话页。
- 同一 task 下继续发送第二轮消息时，客户端不会创建新的 task，而是创建新的 turn。
- 用户消息可以先本地回显，再接收后端流式回复。
- 新建 task 发送后，左侧 workspace 树下会立即出现对应 task 条目，并在后端返回正式数据后完成替换或校正。
- 第二轮对话不会清空历史。
- tool / status / message 能按后端 sequence 顺序显示。
- 每次新一轮对话都能分配新的 `turnId`。
- 每次新 turn 都能通过 `turnId` 独立订阅实时 stream。
- 重连同一个 `turnId` 不会导致重复执行，只会接入已有流或回放已有事件。
- 删除 workspace 后，其下 task 与相关记录全部删除。
- `markdown / tool / status` renderer 可正常工作。

## 变更记录

- 2026-07-17：创建文档，沉淀关于对话页 timeline、结构化 event 和多 renderer 架构的初步讨论。
- 2026-07-17：补充客户端交互要求：点击“新建任务”进入专门界面、先选 workspace、首条发送即创建 task、workspace 下可有多个 task、task 支持多轮对话，以及客户端本地回显要求。
- 2026-07-17：补充左侧栏回显要求：新建 task 后 sidebar 需立即出现临时 task 条目，并与主区共享同一创建态。
- 2026-07-17：重整全文结构，按“页面流、数据层级、回显、timeline、event 升级、开放问题、验收方向”重新归档现有讨论结论。
- 2026-07-17：确认左侧临时 task 条目标题默认取首条用户消息摘要。
- 2026-07-17：确认左侧历史 workspace 列表支持折叠，点击历史 task 可继续对话；删除 workspace 时连同其下 task 记录一并删除。
- 2026-07-17：确认后端去掉 `session`，核心实体边界收敛为 `workspaceId / taskId / turnId`，其中 `turnId` 表示一次用户发起输入后到本轮收束为止的完整运行单元。
- 2026-07-17：确认 `message / event / timeline` 边界：message 是可显示内容单元，event 是运行事实，timeline 是两者的显示投影。
#+#+#+#+functions.exec_command to=functions.exec_command  天天中彩票会json ംഗылара{"cmd":"sed -n '260,520p' docs/前后端改造实施清单.md","workdir":"/Users/woaigugu/Documents/coding-agent","yield_time_ms":1000,"max_output_tokens":20000} աշխատոըമ്പിണেউრც.functions.exec_command.commentary to=functions.exec_command  เงินฟรีChunk ID: b77cb5
