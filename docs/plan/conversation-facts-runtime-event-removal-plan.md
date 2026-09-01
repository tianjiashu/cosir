# 以会话事实替代 RuntimeEvent 的后端重构计划

## 1. 决策与目标

本计划在绿地开发期移除 `RuntimeEvent`、`runtime_events`、`RuntimeEventBus` 与
`TurnStreamService` 这套 Runtime 通用事件链路，不保留兼容端点、双写、旧 SSE 或旧事件重放。

**前置决议：**根 `AGENTS.md` 当前仍规定 `RuntimeEvent` 保留为审计、诊断和可观测性旁路，
与本计划的彻底删除目标冲突。本计划不在未确认的情况下修改该长期决议：进入实施前，必须由
项目决策将该条改为“使用结构化日志/可观测性 trace（必要时独立 `AuditRecord`）承担审计，
不保留 `RuntimeEvent` 领域类型、表和总线”。只有该决议与本文件在同一次变更中一致后，才可
删除 RuntimeEvent 体系。

目标架构是：后端以持久化的会话事实为唯一业务事实；Agent Runtime、工具、审批、委派和
取消均通过同一个领域写入边界改变事实；Assistant Transport 只在 API 边界把这些事实的
快照/变化投影为 UI state。前端只发送命令、渲染服务端 state，不保存或回传对话事实。

本计划不是把现有 RuntimeEvent 换名为新 event 表。新的变化记录只表达“哪个会话事实在
哪个 revision 被写入”，不承载模型 token、工具 stdout 或 Runtime 控制流。

### 完成标准

- Runtime 专用的 `RuntimeEvent`、`EventType`、事件 payload registry、
  `runtime_events`、`RuntimeEventService`、`RuntimeEventBus`、`TurnStreamService` 均已删除。
- API 层不再 import Runtime/Tool/Delegation 的事件类型或 payload；不存在
  `_apply_runtime_event` 一类 UI event projector。
- 任一对话在运行中、刷新后、重新连接后，均从相同的 canonical conversation facts 看到
  相同的消息、工具、审批、委派、运行状态与失败原因。
- 取消、成功、失败各只通过一个事务性领域入口落定一次终态；重复请求、客户端断连和
  Runtime 异常不会覆盖已经落定的终态。
- Assistant Transport 官方请求结构允许携带 `state`；本项目在前端发送前剥离它，后端也不
  采信它。该项目策略保证请求只以 command、task/thread 身份、模型调用配置与版本前置条件
  作为业务输入，响应只由服务端 canonical state 投影产生。

## 2. 当前代码事实与问题

### 2.1 当前实时和历史状态不是同一来源

当前实时链路：

```text
AgentRuntime
  -> RuntimeEventService / RuntimeEventBus
  -> TurnStreamService
  -> app.api.assistant_api._apply_runtime_event
  -> assistant-stream state operations
```

当前历史链路：

```text
turns + turn_messages
  -> ConversationStateService
  -> AssistantTransportState
```

`assistant_api.py` 仅将 `RUN_*`、`MODEL_OUTPUT_DELTA` 和 `FINAL_RESPONSE` 投影到
Transport state；工具输出、推理增量、委派、上下文占用、文件变更等事件未形成完整的 UI
事实。`ConversationStateService` 又依赖 `TurnRecord.response_text` 与 `turn_messages`
fallback 恢复历史。因此刷新、断线和实时运行可能得到不同的视图。

### 2.2 RuntimeEvent 承担了互相冲突的职责

`RuntimeEvent`/`EventType` 既作为 Runtime 控制流返回值，又作为持久化审计记录、进程内
广播消息、前端时间线输入和部分状态机触发条件。`RuntimeEventBus` 的有界队列还允许丢弃
非终态事件；这与“它能重建 UI”相冲突。

实际消费者还包括：

- `core/runtime/runner.py`：运行、异常、取消、文件稳定事件；
- `service/tool_execution/tool_execution_service.py`：工具开始/结束、流式输出、文件更新；
- `core/delegation/child_agent_runner.py`：根据 child RuntimeEvent 判断结果；
- `core/context/context_listener/*`：把内部 context listener 结果再写为 RuntimeEvent；
- `service/task/change_set/operations.py`：直接发布文件更新事件；
- `api/assistant_api.py`：逐事件投影 UI。

这使新增一种能力需要同步修改 Runtime、payload registry、bus、存储、API projector 和前端，
违反单一事实源。

### 2.3 官方 Assistant Transport 的边界

Assistant UI 官方 Assistant Transport 以“前端 command -> 后端 Agent state snapshots”为
模型，state 可以是任意 JSON。`assistant-stream` 根据服务端 state 的变更生成 `set` 与
`append-text`；前端 converter 仅把 state 映射为 UI 消息。协议不要求 RuntimeEvent，且
明确将 UI 视为 Agent state 的无状态视图。

项目当前的 `useAssistantTransportRuntime` 已使用该模式，并在
`prepareSendCommandsRequest` 中剥离前端 state；此方向应保留并完成服务端事实化。

参考：<https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport>

官方请求形状包含客户端 `state: T`。本项目选择在前端
`prepareSendCommandsRequest` 与后端 API 边界剥离或忽略该字段，使服务端 canonical facts
成为唯一事实源；这是项目级安全和一致性约束，不是 Assistant Transport 的官方限制。该行为
必须由前后端契约测试固定。

## 3. 目标模型与架构边界

```text
Assistant Transport command
        |
        v
ConversationCommandService + ConversationRunService  (幂等、占用、创建 Run)
        |
        v
Agent Runtime / Tool Runtime / Delegation / Approval
        |
        v
ConversationMutationWriter  ----------------------> ConversationChange (revision/outbox)
        |                                               |
        +--> ConversationMessage / MessagePart          +--> active-stream notifier
        +--> ConversationToolCall / ToolResult
        +--> ConversationApproval / Delegation
        +--> ConversationRun (状态、终态、错误)
        |
        v
ConversationStateService (只读 canonical projection)
        |
        v
Assistant Transport API -> assistant-stream -> frontend converter
```

### 3.1 领域事实

新增/重构后的持久化模型以 `Task` 为聚合边界：

| 事实 | 建议表/聚合 | 说明 |
| --- | --- | --- |
| 一次执行 | `conversation_runs`（可由现有 `turns` 演进） | run 状态、模型选择、开始/结束、终态原因、版本。 |
| 用户和助手消息 | `conversation_messages` | 稳定 message id、role、run 归属、创建时间、状态。 |
| 消息内容 | `conversation_message_parts` | `text`、`reasoning`、`tool-call`、`tool-result`、附件引用等有序 part。 |
| 工具执行 | `conversation_tool_calls` | 参数、状态、结果摘要、错误、关联 part。工具 stdout 不作为无限事件流持久化。 |
| 审批与委派 | `human_approval_requests`、`delegations` | 继续使用或演进为可由会话投影读取的领域事实。 |
| 命令幂等 | `conversation_commands` | 保留 command id、payload hash、run 绑定及终态。 |
| 状态变化 | `conversation_changes` | 仅保存 revision、mutation 类型、受影响实体 id、created_at；用于通知/断线恢复，不复制完整 payload。 |
| 对话版本头 | `conversation_heads` | 每 task 一行，保存当前 revision；用于安全、原子地分配版本。 |

`turns`、`turn_messages` 是当前已有事实模型。实施时应一次性重命名/替换为上述明确模型，或
保留物理表但将 Python 领域模型和职责迁移为 Conversation 名称；不得继续让
`response_text` 作为助手完整内容的权威副本。
``
`ConversationMutationWriter` 必须按 task/run 串行化写入。当前工具执行可并行、委派回调和
部分 Runtime 回调可跨线程到达；它们不得直接并发改消息、状态或 revision。Writer 需要通过
单 run 写入队列/锁及数据库事务获得确定顺序。模型文本按合理大小或时间窗口批量落库，避免
每个 token 产生一次事务；`append-text` 只是 Transport 优化，永远不是持久化事实格式。

### 3.2 ConversationMutationWriter

新增 `ConversationMutationWriter` 到 `service/task/`（或重构后的
`service/conversation/`）并作为唯一写入入口。它必须提供意图明确的方法，例如：

- `start_run_with_user_message(...)`
- `start_assistant_message(...)`
- `append_assistant_text(...)`
- `append_reasoning_text(...)`
- `start_tool_call(...)` / `complete_tool_call(...)`
- `record_approval_request(...)` / `resolve_approval(...)`
- `start_delegation(...)` / `finish_delegation(...)`
- `complete_run(...)` / `fail_run(...)` / `cancel_run(...)`

每个方法在同一数据库事务中：写入领域事实、递增任务会话 revision、插入一条
`conversation_changes`。写入失败时 Runtime 必须收到异常并停止继续产生依赖该写入的状态。
通知在提交后触发，通知失败不得回滚已提交的领域事实。

revision 分配由 `conversation_heads(task_id PRIMARY KEY, revision)` 实现：同一 SQLite 写事务
中条件更新/递增 head、写入事实、插入带唯一 `(task_id, revision)` 的 change。初始化、冲突
重试和失败回滚均在 Writer 内封装；禁止以 `MAX(revision) + 1` 作为分配策略。

### 3.3 ConversationChange 的用途与限制

`conversation_changes` 不是 Event Sourcing，也不是 RuntimeEvent 的兼容表：

- 它不能保存 token delta、工具 stdout chunk 或任意 Runtime payload；
- `revision` 在 `task_id` 范围内严格单调递增，并由数据库事务分配；
- 订阅者只把它当作“重新读取/增量投影 canonical facts”的提示；
- 断线后的正确恢复来自给定 revision 的 canonical state 快照，而不是从内存队列补事件；
- 可以在后续引入 outbox worker，但首版不引入外部消息队列。

Transport projector 收到 change 后重新读取 canonical state。只有能证明当前文本 part 相对其
上一个已确认快照是同一 part 的尾部追加时，才使用 `append-text`；其他情况一律 `set` 完整
替换。最终 state 始终由 canonical snapshot 校验，不能由操作日志反推业务事实。

### 3.4 ConversationRunExecutor 与订阅分离

`TurnStreamService` 目前同时承担“由 HTTP stream 启动 Agent、单 producer 仲裁、订阅事件、
断连失败处理”四项职责。删除它之前，必须先实现 `ConversationRunExecutor` 与
`ConversationRunSubscriptionService`：

- `ConversationRunExecutor` 通过数据库 lease 认领一个 pending/recoverable run，并独立于 HTTP
  请求在后台驱动 Agent Runtime；一个 run 在任意时刻只允许一个有效 lease。Run 需要持久化
  `executor_lease_owner`、`executor_lease_expires_at`、递增 `lease_fencing_version`、
  `workflow_version` 等恢复所需元数据。
- claim、续租、Writer 变更、终态落定均携带并条件校验 fencing version；lease 过期后重新认领的
  executor 获得更高版本，旧 executor 即使线程仍在运行也不能再提交任何事实。应用启动 recovery、
  后台 lease 续租/轮询、优雅关闭释放与进程崩溃后的过期认领均由 Executor 统一负责。
- HTTP/Assistant Transport 仅订阅 run 所属 task 的 committed conversation changes，绝不成为
  Agent 的生命周期拥有者。客户端断开时执行继续；只有显式 cancel 才请求停止。
- 进程启动时 recovery worker 检查过期 lease：已取消的 run 不启动；可恢复的 run 依据 checkpoint
  重新认领并恢复；不可恢复的 run 以稳定的 `runtime_recovery_failed` 原因落定失败。不得让
  `pending/running` 永久悬挂。
- 重复 command、重复订阅或多个窗口只能附着到既有 run，不能再次启动 Agent；claim 的条件更新与
  lease 所有者是唯一执行权威。

Transport subscription 的可执行算法如下：

```text
注册 notifier 订阅
-> 在同一一致性读取中获取 canonical snapshot 和 revision R
-> 再查询 revision > R（覆盖订阅建立窗口）
-> 发送 snapshot / 更新 last_sent_revision
-> 等待 notifier 或短周期轮询
-> 查询 revision > last_sent_revision
-> 逐条投影，或直接发送最新完整 snapshot
```

事实、`conversation_changes` 与 revision 在同一写事务中提交，提交成功后才触发 notifier。进程在
提交后、通知前崩溃不会丢事实：下一次轮询、重连或 recovery 都会查询数据库 revision。若 change
被清理、revision 有缺口或 projector 无法证明增量安全，则在同一一致性读取中发送当前完整 snapshot；
每次发送 snapshot 后再次补查期间新增 revision。`last_sent_revision` 只能前进，重复通知允许产生
幂等的重复 state 设置，绝不允许 revision 倒退。

## 4. 执行与取消语义

### 4.1 发送消息

1. Assistant Transport 的 `commands` 是有序数组。一次普通发送通常只有一条 `add-message`，但
   后端必须按顺序校验和处理完整数组，支持 `add-message` 与已注册自定义命令；
   不得静默丢弃额外命令。每条命令都有独立 command id、payload hash 和幂等记录。
2. `ConversationRunService` 在事务中处理该批命令：新 `add-message` 创建/绑定 run、用户消息和
   初始 assistant 消息；其他已注册命令只改变其已归属 run 的 canonical facts。
3. 重复命令若关联 run 正在运行，API 返回/附着该 run 的活动订阅（含 runId），而不是伪造新的
   静态成功快照；不同 payload hash 返回 409。
4. API 取得该 task 的 canonical state snapshot，通知 Executor claim 或复用既有 run。
5. Runtime 的模型/工具/审批/委派动作调用 `ConversationMutationWriter`，而不是 `yield`
   RuntimeEvent。
6. Transport streamer 基于已提交的 revision 读取 state 并更新 `controller.state`。只有 projector
   能证明文本 part 相对上一已确认快照为严格尾部追加时才产生 `append-text`；否则包括所有非文本
   结构变化在内均产生 `set`。

### 4.2 取消、断连与终态

- `POST /runs/{run_id}/cancel` 先通过 `ConversationMutationWriter.cancel_run` 以条件事务保证
  仅 `pending/running -> cancelled` 一次，再通过 Runtime Control Port 向活动执行发送协作取消
  信号。信号失败不撤销已提交的取消事实。
- Runtime 在模型 token 循环、工具调度前后、delegation 等待点检查持久化取消标记；内存
  cancellation registry 只能作为加速手段，不能是事实源。pending run 在 claim 时必须再次进行
  条件状态校验。
- `assistant-stream` 的 `controller.is_cancelled` 只表示本次 HTTP stream 已关闭；它不能自动等同
  于用户取消。客户端显式停止必须调用 cancel API。断连只取消该订阅，除非产品以后明确提供
  “断连即取消”的业务策略。
- Runtime 的 `finally` 只做资源清理和“若仍 active 则按明确原因失败/取消”的条件落定；不能
  直接覆写已完成、已取消或已失败的终态。
- 父 run 取消时，child run、delegation、approval 的级联规则由领域服务统一处理；HTTP stream
  断开不自动等同于用户取消。
- 成功/失败/取消都通过 MutationWriter 的单一条件转移完成，同时终结 command；API 只投影结果，
  不再调用 `mark_status`。

新 cancel API 的完整顺序固定为：持久化条件取消事务提交 -> `RuntimeControlPort.request_cancel`
通知当前 executor -> Agent 在模型/工具/委派/审批等待点协作退出 -> executor 用条件终态转移收束。
Runtime 不在当前进程或 lease 已失效时，持久化取消仍然有效；后续 claim/recovery 必须拒绝启动它。
Graph 停在 `interrupt()` 时，executor 检查取消事实后不发送 `Command(resume=...)`，并将未决审批
按定义的级联规则关闭。新 `/runs/{run_id}/cancel` 和前端调用必须先完成端到端验证，再在同一次
破坏性切换中删除 `/turns/{turn_id}/cancel`、`cancel-turn.ts` 的旧调用及相关测试。

### 4.3 工具、文件、上下文、委派与审批

- 工具开始/结束与结果成为 `ConversationToolCall` 及对应 message parts；展示需要的结构化摘要
  持久化，原始大输出继续受既有预算/截断规则控制。
- 文件写入继续属于 ChangeSet；会话仅保存可展示的变更引用和摘要。实时文件进度可作为临时
  `run` 子状态，不写成通用事件日志。
- Context listener 保留为 Runtime 内部观察机制；context usage 若需 UI 展示，写为 run 的
  有限统计字段，而不是 `CONTEXT_USAGE` EventType。
- Delegation 的父子关系和状态仍是服务端领域事实；父会话将其投影为 delegation/tool-like part。
  Child runner 应改为读取 child run 的 canonical terminal state/结果，而不是消费 child events。
- Approval 必须作为持久化请求和决策事实；Transport 可用自定义 command 提交决策，但不得由
  前端直接修改领域状态。
- `TurnRuntimeMessageStore`、`RuntimeContextManager` 和 `TurnService` 对消息的读写必须迁移到
  canonical message/part Writer 或其低耦合 port；不能删除事件后继续绕过 Writer 写
  `turn_messages`。LangGraph checkpoint 继续是 Agent 执行状态、节点恢复和 interrupt 的事实，
  不是 Chat UI 事实；它不能替代 Approval 或 Conversation Facts。`response_text` fallback 仅在
  消息事实迁移并完成验证后删除。

### 4.4 LangGraph checkpoint、审批和运行身份

`Task.id` 是产品的 Conversation Thread 身份；`ConversationRun.id` 是一次具体 Agent 执行身份。
当前 ReAct workflow 使用 `turn.id` 作为 LangGraph `thread_id`，重构后明确改为使用
`ConversationRun.id`，使一个 run 恰好对应一个稳定 checkpoint thread。不得以 `task_id` 复用多个
run 的 checkpoint。

- Executor 每次启动或恢复都从持久化 run 重新构造不可序列化的 `RuntimeConfig`、模型、工具、
  Writer 与 RuntimeControlPort；这些对象不得写入 checkpoint。
- Approval decision 先以幂等 command/mutation 落为领域事实，再对相同 checkpoint `thread_id`
  执行 `Command(resume=...)`。因为 LangGraph interrupt 恢复会从节点开头重跑，interrupt 前的
  所有外部副作用必须经 command id、tool call id 或 writer 条件写入保证幂等。
- `workflow_version` 写入 run。图结构或节点语义发生破坏性升级时，不尝试用新图继续旧的暂停
  checkpoint；绿地开发期将这些 active run 显式终结为 `runtime_version_retired`，并清理对应
  checkpoint 后再升级。

## 5. API 与 Transport 协议

### 5.1 保留的端点形态

- `POST /assistant`：接收 Assistant Transport command，创建或复用 command/run，流式投影服务端
  canonical state。
- `GET /tasks/{task_id}/assistant/state`：读取某 task 的 canonical state 及 revision。
- `POST /runs/{run_id}/cancel`：显式取消运行。现有 `/turns/{turn_id}/cancel` 在重构时删除，不保留
  别名。

请求仅将 `commands`、`taskId`、模型调用配置和可选 `stateRevision` 作为业务输入；即使 wire
request 仍携带官方 `state` 字段，后端也必须忽略它，绝不把它当作事实。`threadId` 与 `taskId`
的映射规则必须明确为 Task 是唯一 Conversation Thread；若两者都保留，服务端必须校验二者
一致，否则删除 `threadId` 的业务使用。

### 5.2 State 形状

Transport state 是 API 边界 DTO，不进入 core/service/storage。建议至少为：

```json
{
  "revision": 42,
  "messages": [
    {"id": "...", "role": "user", "parts": [{"type": "text", "text": "..."}]},
    {"id": "...", "role": "assistant", "status": "running", "parts": [
      {"type": "text", "text": "...", "status": "running"},
      {"type": "tool-call", "toolCallId": "...", "toolName": "...", "args": {}, "status": "complete"}
    ]}
  ],
  "run": {"runId": 7, "status": "running", "cancelRequested": false},
  "error": null
}
```

`assistant-stream` 负责把 state 变更编码为 `set` / `append-text`。这些操作不应成为领域模型、
数据库模型或业务服务的依赖。

### 5.3 重连

当前前端只配置 `api`，未配置 `resumeApi` 或 `resumeStateApi`。页面刷新后 GET state 只能水合
快照，不能自动恢复正在执行的 HTTP stream。首版可以明确仅支持快照恢复；若产品要求恢复
活动流，必须实现 `resumeApi` 或等价的项目订阅端点。`resumeStateApi` 仅在采用服务端保留的
活动 run 起始快照时实现，属于可选能力：

1. 前端先重新 GET canonical state；
2. 可选的 `resumeStateApi` 按官方语义接收 `{threadId}` 并返回服务端保留的起始
   `{runId, state}`；本项目可以附加 `revision`，但它仍是快照端点；
3. `resumeApi` 用于恢复活动流，接收 `taskId`、`runId`、`afterRevision` 并返回 Assistant Transport
   stream，不能返回普通 JSON snapshot；
4. 服务端校验 run 身份和 revision，随后从当前 canonical state 补齐投影；不从 RuntimeEventBus
   或客户端 state 补放事件。

active-stream notifier 只是唤醒提示，不保证送达。每个 Transport stream 维护
`last_sent_revision`，以数据库查询 `revision > last_sent_revision` 作为真实补齐依据；通知丢失
或订阅建立竞态不得使流永久停滞。无法安全增量补齐时，服务端发送完整 state snapshot 与当前
revision。项目既有 `GET /tasks/{task_id}/assistant/state` 只是自定义当前快照端点，不等价于
`resumeApi` 或 `resumeStateApi`。官方 `resumeStateApi` 的 `{runId, state}` 语义和本项目
`afterRevision` 语义必须分别记录，不能相互冒充。

## 6. 分阶段实施与删除清单

### 阶段 0：确认长期决议与隔离 Workspace 事件

1. 先更新根 `AGENTS.md` 的 RuntimeEvent 审计决议，确认用结构化日志/trace 或独立 AuditRecord
   替代 RuntimeEvent 后才进入彻底删除路线。
2. 保留 Workspace prepare/SSE 产品能力，拆出 `WorkspaceEventType`、payload base、持久化
   readiness snapshot 和 GET 恢复契约。
3. 建立生产者—消费者—持久化—DI—测试—级联删除矩阵，并冻结旧 RuntimeEvent 的新增使用。

### 阶段 A：建立新事实模型和事务边界

1. 设计并创建 conversation message/part/tool-call/change 表及 CRUD、领域值对象。
2. 引入 `ConversationMutationWriter`、revision 分配和事务性 command/run 创建。
3. 引入 ConversationRunExecutor、Run Control Port 和数据库 lease；定义恢复、重复 claim 与断连语义。
4. 将 `Turn` 重命名/演进为 `ConversationRun`；将 `TurnMessage` 迁移为 canonical message/part。
5. 重写 `ConversationStateService`，只读取上述事实并移除 `response_text` fallback。

验收：无 RuntimeEvent 参与时，能创建用户消息、助手占位、写入文本、工具调用、完成/失败/取消，
并通过 GET state 完整恢复。

### 阶段 B：迁移 Runtime 写入

1. `AgentRuntime` 改为调用 MutationWriter，不再返回/生成 `RuntimeEvent`。
2. ToolExecutionService、ChangeSet、Context Listener、Delegation、Approval 逐个迁移到明确的
   writer 方法或私有 Runtime callback。
3. ChildAgentRunner 改为以 child ConversationRun 终态和持久化结果判定委派结果。
4. 取消 registry 改为持久化取消事实优先、Runtime Control Port 内存信号加速。
5. 将 checkpoint thread 从旧 `turn.id` 明确迁移为 `ConversationRun.id`，并完成 approval/
   `Command(resume=...)`、workflow version 和 checkpoint 清理策略。

验收：模型、工具、文件改动、委派、异常、取消均能在刷新后重建，且不会依赖 event payload。

### 阶段 C：替换传输与重连

1. 删除 `assistant_api._apply_runtime_event`；Assistant API 改订阅 conversation changes 并投影 state。
2. 在 RunExecutor 已通过 lease/recovery/取消验证后删除 `TurnStreamService`；运行启动与 stream
   订阅分别由 Executor 与 Transport subscription service 承担。
3. 增加 active run state GET/订阅/恢复契约及 revision 测试。
4. 前端 converter 扩展 message parts 的完整渲染，但不增加领域状态推导。

验收：断连、刷新、双标签页和重复 command 情况下，所有客户端视图最终收敛到相同 revision。

### 阶段 D：删除旧事件体系

删除以下 Runtime 专用内容及其 DI、级联删除、response schema、测试、文档引用：

- `models/event/runtime_event.py`、已完成 Workspace 拆分后的 Runtime 专用 `EventType`；
- `models/payload/runtime_event_payload.py` 及 payload registry、专用 event payload；
- `storage/model/runtime_event_model.py`、`storage/crud/runtime_event_crud.py`、`runtime_events` schema；
- `service/agent_runtime_event/`；
- `service/task/turn_stream_service.py`；
- 所有 `yield RuntimeEvent`、`write_event`、`get_runtime_event_*`、事件型 SSE/projector；
- 旧 `/turns/{id}/cancel` 端点及旧测试。

`ContextEvent` 不应被机械迁移：若只在 Context 聚合内部同步调用，可以保留为私有领域通知；若要
跨进程或驱动 UI，则设计 Context 自己的事实模型，不能复活通用 RuntimeEvent 总线。

Workspace 的决议是**保留**现有 `GET /workspaces/{workspace_id}/events/stream` 与
`POST /workspaces/{workspace_id}/events/prepare` 的产品能力，但将其从 Runtime 体系中拆出：

- 新建独立 `WorkspaceEventType`、`WorkspacePayloadBase`、Workspace event schema 与 service，
  迁移 workspace API/SSE、payload registry 和测试，不再借用 `EventType` 或
  `RuntimeEventPayload`。
- 新增持久化 `WorkspaceReadiness` snapshot，并提供 `GET /workspaces/{workspace_id}/readiness`：
  响应至少含 `status`、`reason`、`updatedAt`、`revision`。`prepare` 以 workspace 级条件状态或
  独立 command id 幂等化；并发 prepare 合并为同一次准备或返回同一进行中的 readiness。
- prepare 的写入顺序固定为“事务写入 WorkspaceReadiness -> 事务提交 -> 发布 SSE 唤醒通知”；SSE
  仅作为“状态可能变化”的通知，不作为事实或重放来源。
- SSE 连接、断线和队列溢出后，客户端通过 workspace readiness GET 重新水合；WorkspaceEventBus
  可以作为本地通知优化，但不能保证可达，也不得混入 `ConversationChange` 或 Assistant Transport。

完成此隔离后才能删除 Runtime 专用的枚举和 payload 基类。

删除前必须维护“生产者—消费者—持久化—DI—测试—级联删除”迁移矩阵。至少涵盖：
`core/workflows/react/workflow.py`、nodes 的 common/model/observation/tools、
`core/runtime/runtime_operations.py`、`core/runtime/runner.py`、ToolExecutionService、
DelegationService/ChildAgentRunner、Context listener、TurnRuntimeMessageStore、API response schema、
`api/dependencies.py`、`service/depends.py`、`CascadeDeleter`、TaskService、WorkspaceService 及其测试。
不得仅依据 `rg RuntimeEvent` 删除表面引用。

## 7. 验收测试矩阵

必须新增并通过以下测试（除真实 LLM 测试外均为普通 pytest）：

- command 幂等：相同 `commandId + payload hash` 不创建第二个 run；不同 hash 返回冲突。
- 原子性：command、run、用户 message、assistant 占位、revision/change 任一失败时整体回滚。
- 文本流：多次 append 后，GET state 与实时 stream state 内容和 revision 一致。
- 工具流：开始、参数、完成、错误、截断输出在重连后仍能完整显示。
- 终态：成功、Runtime 异常、显式取消、取消与成功竞争、断连均不覆盖既有终态。
- executor：重复 claim、lease 过期、HTTP 断连、进程重启恢复、取消后 claim 均只允许定义的一个
  执行者推进 run。
- 委派：child 成功、失败、取消时父 run 和 parent message part 正确落定。
- 审批：等待审批、恢复、拒绝、取消期间审批的状态一致。
- checkpoint：同一 run 使用稳定 checkpoint `thread_id`；`Command(resume=...)` 重放不会重复工具
  副作用；workflow version 不匹配的暂停 run 被显式终结而非误恢复。
- 重连：运行中刷新、两个订阅者、消费者慢于生产者时不丢 canonical UI 状态。
- notifier：提交后通知丢失、订阅建立竞态、revision gap 与 change 清理时，stream 均能通过 snapshot
  收敛且 revision 不倒退。
- workspace：Workspace SSE 与 readiness GET 经过断线/队列溢出后仍可由持久化 snapshot 恢复，且
  不依赖 Runtime `EventType`/payload。
- 删除验证：仓库中不存在 Runtime 专用 `RuntimeEvent`、`EventType`、`runtime_events`、
  `TurnStreamService` 的生产引用；schema 初始化与级联删除不再注册旧表。Workspace 事件已经独立
  迁移，不再借用 Runtime 类型或 payload。
- Assistant Transport 契约：POST response 为 `assistant-stream` state operations；官方 wire 虽允许
  state，但本项目前端剥离它、后端忽略它；converter 只映射 state，无客户端业务状态写入。

## 8. 风险与约束

- 不进行双写或旧链路回退：绿地阶段若保留会再次制造两份事实。每个阶段应在可运行、测试通过后
  直接切换并删除对应旧实现。
- 迁移前先列出所有 RuntimeEvent 的 producer/consumer；不得只删除 API projector。
- SQLite 的 revision 分配必须在写事务中实现，不能用“读最大 revision + 1”的无锁方式。
- 长文本和工具输出必须继续遵守预算，不能因从 event 改为 message part 而无限写库。
- API 层可持有 Assistant Transport DTO 与 stream controller；`core`、`service`、`storage` 禁止
  import `assistant_stream` 或 `assistant-ui` 类型。
