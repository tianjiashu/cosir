# Conversation State：以 Context 为唯一消息事实源、按需重建 Snapshot

> 状态：已完成独立子Agent复核，并已整合复核意见。
> 本文是基于当前源码的改造方案，不是对附件中其他方案的执行指令；源码和项目架构边界优先于本文。

## 1. 决策摘要

本次重构只保留三类持久化事实源：

| 事实层级 | 唯一持久化载体 | 负责内容 |
| --- | --- | --- |
| Task 级 | `TaskModel` | Task 身份、父子/分叉关系、当前 Run、上下文用量及上下文窗口 |
| Run 级 | `ConversationRunModel` | Run 身份、状态、结束原因、输入、最终输出、模型路由、usage、错误 |
| message/tool 级 | `ConversationTaskContextModel` | 所有可恢复的 system/user/assistant/tool 消息、消息顺序、消息归属及工具 UI 元数据 |

`ConversationStateSnapshot` 仍然可以作为 Assistant Transport 的内存读模型，但不再写入
`conversation_task_snapshots.state_json`。冷读、重连和进程重启后的首次读取，统一从
`TaskModel + ConversationRunModel + ConversationTaskContextModel` 重建。

这里的“单一事实源”具体指：

- 消息和工具结果只在 `ConversationTaskContextModel` 保存一份；
- Run 生命周期和 Run 级执行结果只在 `ConversationRunModel` 保存一份；
- Task 级当前选择和用量只在 `TaskModel` 保存一份；
- 内存 snapshot、SSE 事件、LangGraph checkpoint、文件 ChangeSet 都不是业务事实源。

这不是要求取消所有内存状态。活跃流式 Run 仍需要进程内 working copy 来承载尚未完整落库的
delta；它在重启后丢弃，并按持久化事实重新开始，而不是隐式重放。

### 1.1 持久化优先与最终一致性

所有会成为业务事实的 message、tool result、Run 终态、Task 用量和 Task 当前 Run，都遵循
同一条时序：

```text
数据库事实持久化成功
        ↓
更新进程内 Transport working state（内存 snapshot）
        ↓
发布 SSE / 通知订阅者
```

这里的 snapshot 不是数据库快照，而是 Assistant Transport 的进程内读模型。数据库写入和
内存 snapshot 更新之间**不建立事务，也不要求强一致**。数据库提交成功后，即使 snapshot
更新或 SSE 发布失败，后续 GET、attach 或重连仍可从三个模型重建并最终收敛；失败必须记录
结构化日志，不能回滚已经提交的业务事实，也不能让 snapshot 反过来覆盖数据库。

## 2. 当前代码事实与问题

### 2.1 当前已经存在的事实

- `ConversationTaskContextModel` 位于
  `apps/backend/app/storage/model/conversation_task_context_model.py`，一行保存一个
  LangChain message JSON，并带有 `task_id`、可空 `run_id`、`include_in_context`、
  `sequence`；`(task_id, sequence)` 唯一。
- `ConversationTaskContextService` 已经负责 append、读取 context、fork clone 和中断工具
  修复；它应成为所有 context message 写入的统一入口。
- `ConversationRunModel` 已经保存 `status`、`end_reason`、`input_text`、
  `final_output` 以及模型/provider 路由字段，但当前没有独立 usage/error 字段。
- `TaskModel` 已经保存 `context_usage_used`，但当前没有持久化 `current_run_id` 和
  `context_window_total`。
- `ConversationTaskSnapshotService` 当前在
  `apply_planned()` 中读取、校验并全量 UPDATE 整个 `state_json`；流式 event 因此会
  反复写入越来越大的 JSON。
- `ConversationEventProjector` 负责把 conversation event 变成 snapshot mutation；
  它不是 Agent 的事实写入入口。
- `RuntimeContextManager` 会把完整的 AI/tool message 写入 context，但当前重建所需的
  UI part 边界、稳定 message id、历史 presentation 和 tool `display_data` 没有全部落库。
- `RuntimeContextManager.add_message()` 重新构造 `AIMessage` 时只保留
  `content`、`additional_kwargs`、`tool_calls`，会丢失原消息的 id、usage metadata、
  response metadata 等字段；不能把当前实现误写成“完整原生消息已保存”。
- 当前流式状态机允许 text/reasoning 交错，并可能产生多个 part；仅从最终
  `AIMessage.content` 和 `reasoning_content` 无法恢复原始 part 顺序。
- `ToolObservation.display_data` 是 UI 展示数据，`artifact_data` 是文件快照/ChangeSet/
  审计旁路数据；二者不能混为模型消息，也不能把 `artifact_data` 暴露给 Assistant Transport。
- 当前 fork 会复制 context 和 snapshot；snapshot 中存在 source message id 被保留的情况，
  因此不能假设 message id 仅由新 Run id 确定性生成。
- 本方案按绿地项目落地：新数据库直接按目标 schema 初始化，不设计旧数据库兼容迁移。
  本地验证使用新建的 SQLite 数据库；已有开发数据不作为验收输入。

### 2.2 不能继续保留的假设

以下做法与本方案不兼容：

1. 用 ToolRegistry 反查历史工具的 presentation。工具定义可能改变，历史 UI 必须使用执行时
   记录的声明。
2. 用“最新非终态 Run”猜 `current_run_id`。fork、编辑、重启和多次 Run 会使该规则不稳定。
3. 把 `ToolMessage.artifact` 当作 `display_data` 和 `artifact_data` 的总容器。模型消息、
   UI 数据和文件审计数据有不同保密、体积和生命周期边界。
4. 把 snapshot 的全量 UI parts 当作 context 的等价副本。当前两者的结构和写入时机不同，
   不能靠“同一事务双写”实现真正的单一事实源。
5. 为旧数据库增加兼容迁移、legacy fallback 或双 schema 运行路径。本项目按绿地 schema
   初始化，发现旧数据/旧结构时直接报错并要求使用目标 schema。

## 3. 目标数据模型

### 3.1 TaskModel

保留现有 Task 字段，并新增或明确以下 Task 级事实：

- `current_run_id: int | null`：当前被前端选中/展示的 Run。没有 Run 时为 null；fork
  创建后为 null，直到显式创建新 Run。
- `context_usage_used: int | null`：当前 Task context 的已用 token。
- `context_window_total: int | null`：计算该 Task context 用量时采用的窗口上限。
- 可选 `transport_error_json`：只有产品确实需要 Task 级全局错误时才增加；当前代码没有
  稳定的 snapshot error producer，未确认前保持 snapshot 顶层 `error = null`。

`context_usage_ratio` 由 `used / total` 读时计算并限幅，不再单独存储。若窗口上限因模型
配置改变，必须由 Task 用例显式更新并记录新的事实，重建过程不能临时调用
CapabilityService 重新猜测历史值。

### 3.2 ConversationRunModel

保留已有 Run 字段，并增加以下可查询或可审计的 Run 级事实：

- `usage_json`：保存 snapshot 当前使用的 usage 结构；建议使用 typed schema 做读写校验，
  JSON 只作为 SQLite 存储格式。结构与现有 `ConversationRunUsageStats` 对齐，至少包括
  `input_tokens`、`output_tokens`、`total_tokens`、`cache_hit_tokens`、`cache_miss_tokens`、
  `reasoning_tokens`。
- `error_json`：仅保存受控的错误 code/reason/status_hint 等结构，不保存 stack、完整
  provider response、密钥或原始 prompt。

现有 `input_text` 和 `final_output` 的所有权必须明确：它们是 Run 的执行输入/执行摘要字段，
不是 Assistant UI message 的来源。snapshot 和 Agent context 一律不从这两个字段重建；UI 中的
user/assistant message 只从 ContextModel 读取。`input_text` 由创建 Run 的 command 写入，
`final_output` 由 Run 终态用例写入，二者不能被其他路径独立修改。若后续不再需要委派摘要或
快速检索，应删除这两个字段并改为从 ContextModel 查询，不能同时保留一套未定义的同步规则。

`status` 是 Run 生命周期唯一事实源。成功、失败、取消和重启收敛都必须先完成条件 UPDATE，
再发布事件；重复状态迁移不得重复发布。LangGraph checkpoint、内存 registry 和 snapshot 都不能
改变 Run status。

### 3.3 ConversationTaskContextModel

当前 `message_json`、`run_id`、`sequence`、`include_in_context` 保留，并补齐
重建所需的 message/tool 级字段。推荐扩展为：

- `ConversationTaskContextModel.id`：作为该 Context row 的唯一 message identity。由于
  Transport 契约要求字符串，重建时使用 `str(context_row.id)`；不再新增一份
  `transport_message_id`。fork clone 产生新的 Context row id，因此目标 Task 的 message id
  自然使用新 id，tool_call_id 等 message 内部关联仍按 clone service 的规则保持一致。
- `transport_metadata_json`：与该 context row 一对一的 Transport 注释和 UI 展示数据。
  对 AI row 保存最终 part 段序列（text/reasoning/tool-call 及工具声明）；对 Tool row 保存
  执行后的 `display_data`、受控错误短语和必要的展示状态。这里不重复保存 message id，
  id 永远来自 ContextModel.id。
这些字段仍属于同一张 context 表，不建立第二张 message/tool 事实表。边界如下：

| 数据 | `message_json` | `transport_metadata_json` | 其他存储 |
| --- | --- | --- | --- |
| LLM 可消费的 Human/AI/Tool 内容 | 是 | 否 | — |
| AI 原始 tool call 参数 | 是 | 可保存 UI 分段引用 | — |
| text/reasoning/tool-call 的历史顺序 | 否（最终 message 不足以表达） | 是 | — |
| 工具静态 presentation | 否 | 是，执行时冻结 | — |
| 工具动态 `display_data` | 否 | 是，按预算裁剪后保存 | — |
| `artifact_data`、ChangeSet、文件快照 | 否 | 否 | 现有文件变更/审计存储 |

`transport_metadata_json` 是同一条持久化 message 的序列化/展示元数据，不是第二套
Conversation State。它只允许保存重建 Transport 所必需的信息，不允许保存另一个 status
状态机、另一个 message content 或未受控的大段工具输出。

目标 schema 的 metadata 形状至少固定为以下版本化结构；字段名可在实现时使用对应的
Pydantic/dataclass，但不能由调用方自由拼接 JSON：

```json
{
  "schema_version": 1,
  "parts": [
    {"type": "text", "text": "..."},
    {"type": "reasoning", "text": "..."},
    {
      "type": "tool-call",
      "tool_call_id": "...",
      "tool_name": "...",
      "args": {},
      "presentation": {},
      "status": "pending"
    }
  ],
  "tool_result": {
    "status": "success",
    "display_data": {},
    "status_hint": null,
    "error": null
  }
}
```

AI row 必须保存有序 `parts`；Tool row 必须保存 `tool_result`，并通过
`tool_call_id` 关联 AI row 中的 tool-call。工具调用创建时冻结 presentation，工具结算时
写入 display_data/status/error；两次写入必须是同一 context service 的幂等操作。metadata
写入协议和校验器与 model message 的序列化测试一起实现，不能只新增一个数据库列。

要让“context 本身可重建”，system prompt 也必须有持久化记录。当前
`RuntimeContextManager.__post_init__` 只在内存创建 system message，改造时应在 Task/
context 初始化用例中写入 `run_id = null` 的 SystemMessage，并在提示词版本或工具声明
变化时按明确规则生成新 context 记录；不能在冷启动时用当前环境偷偷替换历史 system prompt。

### 3.4 Snapshot 字段与三类模型的对应关系

Snapshot 不增加新的持久化表。以下映射是唯一重建口径：

| Snapshot 字段 | 唯一来源 | 存储/重建规则 |
| --- | --- | --- |
| `runs[].runId` | `ConversationRunModel.id` | 直接转为 snapshot 的整数 `runId` |
| `runs[].status` | `ConversationRunModel.status` | 直接读取；Run status 不从 context 或 checkpoint 推导 |
| `runs[].endReason` | `ConversationRunModel.end_reason` | 直接读取 |
| `runs[].usage` | `ConversationRunModel.usage_json` | 保存 `input_tokens`、`output_tokens`、`total_tokens`、`cache_hit_tokens`、`cache_miss_tokens`、`reasoning_tokens` |
| `runs[].messages` | `ConversationTaskContextModel` | 按 `run_id`、`sequence` 读取后组装；RunModel 的 `input_text`/`final_output` 不作为 message 来源 |
| `messages[].id` | `ConversationTaskContextModel.id` | 使用 `str(context_row.id)`；HumanMessage 直接使用该 row id |
| `messages[].role` | Context 的 LangChain message 类型 | `HumanMessage` → `user`，`AIMessage` → `assistant` |
| text/reasoning parts | Context 的 `message_json` + `transport_metadata_json.parts` | 内容和历史分段来自 Context row，不从流式 event 猜测 |
| tool-call `toolCallId`/`toolName`/`args` | AI Context row 的 `message_json`/metadata | 创建时写入；同一 Run 多个 AI row 按 sequence 合并到 assistant message |
| tool-call `presentation` | AI Context row 的 `transport_metadata_json` | 工具调用创建时冻结，重建时禁止查当前 ToolRegistry |
| tool-call `status`/`error`/`errorCode`/`isError` | Tool Context row 的 `message_json`/`tool_result` | 有 ToolMessage 取持久化结果；无结果时结合 Run 终态收束为 failed/cancelled |
| tool-call `display_data` | Tool Context row 的 `transport_metadata_json.tool_result` | 只保存预算裁剪后的 UI 数据；不保存 `artifact_data` |
| `approvalRequestId` | 当前无持久化事实 | 审批未实现，固定为 `null` |
| `current_run_id` | `TaskModel.current_run_id` | 直接读取，不按“最新 Run”猜测 |
| `context_usage_used` | `TaskModel.context_usage_used` | 直接读取 |
| `context_window_total` | `TaskModel.context_window_total` | 新增并持久化，不能冷读时重新调用 CapabilityService |
| `context_usage_ratio` | TaskModel 的 used/window | 读时计算并限幅，不单独存储 |
| `approvals` | TaskModel（当前无实现） | 当前固定为 `{}`；未来实现时增加 TaskModel 字段 |
| 顶层 `error` | TaskModel 的 Task 级错误（可选） | 当前没有稳定 producer，保持 `null`；Run 错误另存 RunModel |

同一 Run 可能有多条 AI Context row，但当前 Transport schema 对一个 Run 展示一个 assistant
message。因此 assistant message 的 id 取该 Run 按 `sequence` 排序后的第一条 AI row 的
`str(id)`；后续 AI row 只贡献 parts。这条规则必须在重建器和 live projector 中保持一致。
如果 Run 没有任何已持久化 AI row，则冷读不凭空创建 assistant message；活跃流式期间的
临时 assistant 骨架由进程内 Transport state 承担。

## 4. Snapshot 与 Agent context 的重建

### 4.1 统一输入

重建器只读取以下三个 aggregate 的持久化数据：

1. `TaskModel`：Task 元数据、`current_run_id`、context usage/window、Task 级错误；
2. `ConversationRunModel`：该 Task 的全部 Run，按稳定创建顺序读取；
3. `ConversationTaskContextModel`：该 Task 的全部 context rows，按 `sequence` 读取。

不得在重建过程中读取 snapshot 表、LangGraph checkpoint、当前 ToolRegistry 或前端
Assistant runtime state 来补历史事实。工具 registry 只能用于新执行的定义，不能用于历史
snapshot 的反查。

### 4.2 Snapshot 重建规则

建议新增纯函数/无副作用的
`ConversationTaskStateRebuilder.rebuild(task, runs, context_rows)`：

1. 校验所有 Run 和 context row 的 `task_id` 归属；发现孤儿 row 或非法 metadata 直接返回
   结构化重建错误，不能跳过、猜测或静默串到其他 Task。
2. 按 Run 创建顺序生成 `runs[]`；Run id/status/endReason/usage/error 直接来自
   `ConversationRunModel`。
3. 按 `sequence` 将 context rows 分组。SystemMessage 用于 Agent context，但默认不出现在
   Assistant UI；HumanMessage、AIMessage 和 ToolMessage 参与对应 Run 的 UI 重建。
4. 将同一 Run 的 HumanMessage 组装为 user message；同一 Run 的 AI rows 按 sequence 合并
   为 assistant message，并使用已保存的 part 段序列。目标 schema 中 metadata 是必填的；缺失
   或版本不支持时直接失败，不用当前 registry 或最终 content 猜测历史 parts。
5. AI row 中的 tool call 创建 tool-call part；ToolMessage 按 `tool_call_id` 回填对应 part。
   找不到对应 AI tool call 时直接返回结构化数据错误，不能创建无主 UI part。
6. Tool part 的 status、error、display_data、presentation 只来自 context row 的持久化
   metadata 和 ToolMessage 的持久化字段，禁止按工具名重新反查。
7. 顶层 `current_run_id` 直接使用 TaskModel；不使用“最新 Run”推导。顶层 usage 使用
   TaskModel 的 used/window 计算；`approvals` 当前仍为 `{}`；顶层 error 仅在 TaskModel
   有正式 error 事实时填充，否则保持当前契约的 null。
8. 最后调用现有 `validate_snapshot`。校验失败是数据/代码缺陷，必须结构化记录并返回
   可识别的失败结果，不能用空 snapshot 掩盖错误。

重建结果必须是现有
`ConversationStateSnapshot` wire schema；前端 converter、SSE 事件格式和动态后端端口
边界不因本重构改变。

### 4.3 Agent context 重建规则

`RuntimeContextManager` 不再把自己的 in-memory `_entries` 当事实源：

- 初始化时从 `ConversationTaskContextModel` 读取全部需要纳入 context 的 rows；
- 系统 prompt 也必须来自持久化 SystemMessage；
- 只将 `include_in_context = true` 的 message 转换为模型输入；
- UI metadata 不进入模型 messages；
- fresh run、resume、edit、fork 都通过 `ConversationTaskContextService` 读取/
  删除/clone，再由 manager 建立 working copy；
- manager append 完整 message 后立即通过 context service 持久化；chunk 只留在内存；
- ToolMessage 去重、未闭合 tool 修复和 sequence 分配集中在 context service/用例层，避免
  manager 和 CRUD 各自实现一套规则。

因此 context 和 Transport snapshot 可以短暂不一致：模型完整 message 尚未写入时，实时
snapshot 可以暂时显示流式 part；读取 snapshot 时以 RunModel/TaskModel 校正终态和当前 Run。
这属于最终一致性，不通过把数据库写入和内存 snapshot 更新包进一个事务来解决。

### 4.4 活跃 Run 的内存叠加

冷读路径为“数据库重建 → 放入进程内 working copy”。活跃 Run 仍可由
`ConversationEventProjector` 将事件应用到内存 state，供 SSE 实时发送；该 state：

- 不写入数据库作为事实；
- 不跨后端重启恢复；
- 后端重启时先把遗留 active Run 收敛为 cancelled/failed，再读取已提交 context 重建；
- HTTP/SSE 断开只会失去订阅，不自动取消或重放 Run；
- subscriber 注册和首帧读取必须继续在同一把进程锁内完成，避免“注册前事件丢失”。

### 4.5 Workflow checkpoint 边界

三模型只负责业务事实、Agent context 和 Transport snapshot 的重建，不负责恢复任意时刻的
LangGraph 控制流。当前 `react/workflow.py` 的显式 resume 使用
`checkpoint_thread_id`，而 `ReactGraphState` 还包含 pending tool calls、step count、repair
状态等执行控制数据。因此必须把两种恢复分开：

- LangGraph checkpoint 可以继续作为**显式 workflow continuation 的控制状态**，但不得参与
  snapshot/context rebuild，也不得决定 Run status；
- 后端重启时不隐式续跑 active workflow。按项目生命周期规则先收敛遗留 Run，再从三模型
  重建已提交事实；只有用户明确发起 business resume 时，才允许进入 checkpoint continuation
  用例；
- fork 不得让克隆 Run 共享源 Run 的 `checkpoint_thread_id`。历史克隆 Run 默认不可直接从
  源 checkpoint 续跑；如需继续执行，应创建新的 Run，并从目标 Task 的 ContextModel
  构造输入，生成新的 checkpoint thread；
- 如果未来要完全移除 checkpoint resume，必须另立方案把 workflow 控制状态持久化到明确的
  Run/Context 事实中，不能把三模型重建能力误称为 workflow 恢复能力。

## 5. 写路径改造

### 5.1 创建 Run

创建 command、Run、Task.current_run_id 和初始 user message 的事务由 Run 用例统一保护。
当前 user message 在 workflow 中较晚才 append；需要把“初始用户输入写 context”前移到
创建 Run 的用例，或提供同等原子性的 prepare 阶段，并给 workflow 保留幂等检查，避免重复
写入。

### 5.2 AI message

模型节点可以继续流式产生 event，但只在完整 AI message 边界通过 context service 写入：

- `message_json` 保存模型需要的完整语义字段；
- `transport_metadata_json` 保存真实发生的 part 段序列及工具声明；message id 统一取
  `ConversationTaskContextModel.id`，不在 metadata 中重复保存；
- usage 同时累积到 Run working state，Run 终态时与 status/final_output 在同一 Run 用例中
  持久化；
- 不能在 manager 中重新构造 AIMessage 时丢弃 id、usage metadata 等字段；目标 schema
  必须保存重建所需的完整语义字段。

完整 AI message 的 canonical context 写入成功后，才允许把对应 parts 合并到进程内 Transport
state 并发送实时 mutation。流式 delta 可以先存在内存 working state，但不能以 snapshot
更新成功代替 context 持久化成功。

### 5.3 Tool settle

工具完成、失败或取消时，只写一条 Tool context row（同一 `tool_call_id` 幂等）：

- `message_json` 保存面向模型的 ToolMessage content、tool_call_id 和受控 status；
- `transport_metadata_json` 保存经过预算限制的 display_data、presentation、status_hint；
- `artifact_data` 继续走文件变更/ChangeSet/审计路径，不能放进 Assistant Transport；
- 先将 ToolMessage 和 tool metadata 持久化成功，再由 projector 更新内存 Transport state
  并生成实时 Tool status event；事件失败不能回滚已完成的业务工具结果，也不能杀死 Agent
  主流程。

推荐的工具状态重建规则：

| 持久化事实 | Tool part status |
| --- | --- |
| ToolMessage status = success | completed |
| ToolMessage status = error | failed |
| 有 tool call、无结果，Run = cancelled | cancelled |
| 有 tool call、无结果，Run = failed | failed |
| 有 tool call、无结果，Run 仍 active | 仅在进程内 working copy 中显示 running/pending |

### 5.4 Run 终态、错误和用量

Run service 先条件更新 `ConversationRunModel`，数据库事实提交成功后再更新内存 Transport
state，并发布 `RunStatusChangedEvent`。usage/error 不再只存在 snapshot mutation；如果没有稳定的
错误结构，不能为了填满契约而从任意异常文本猜测顶层 error。

后端恢复时：

- active Run 标记为 cancelled，`end_reason = runtime_restarted`；
- 遗留 active delegation 标记失败；
- 对已开始但没有 ToolMessage 的 tool call 写入受控错误 ToolMessage 或持久化修复记录；
- 不隐式重放 workflow，不复制旧 snapshot。

## 6. 读路径、投影与删除 snapshot

### 6.1 读路径

以下入口统一改为“内存 working copy 命中则读内存，否则三模型重建”：

- `GET /assistant/state`；
- SSE `subscribe_with_snapshot` 首帧；
- attach/resume 前的状态读取；
- workspace/task 切换后的冷读；
- fork 完成后的目标 Task 读取。

仅用于校验命令的数据尽量直接读取 Run/context service，不为了校验重新物化 snapshot。

### 6.2 ConversationEventProjector

projector 继续负责实时 Transport mutation，但删除对 snapshot 表的 upsert：

- 事件只更新进程内 state 和 subscriber；事件处理前必须已有对应数据库事实，不能由事件
  mutation 先行创造持久化事实；
- 不把事件重新当作持久化 message 事实；
- 事件处理失败必须记录 task_id/run_id/trace_id 和 event 类型，按设计选择继续发送或标记
  transport degraded，不得让异常穿透 workflow；
- 消息事实由 context service 写入，避免 projector 与 runtime 双写。

### 6.3 目标 schema 与表删除

分阶段完成：

1. 修改 model/schema 定义，使新建数据库不再创建 `conversation_task_snapshots`，并移除
   snapshot model、CRUD 和注册项；
2. 让新写路径完整保存 context metadata、Run usage/error、Task current_run/window；
3. 实现三模型重建器和全新 fixture；不读取旧 `state_json` 做运行时对照；
4. 切换 GET/SSE/attach/fork 读路径；
5. 删除所有 snapshot upsert、clone 和 `ensure_state_snapshot` 的持久化副作用；
6. 若本地开发目录仍有旧数据库，测试脚本显式要求使用目标 schema 的新数据库；应用启动不
   自动执行破坏性清库，也不承担旧库兼容责任。

这一步涉及的代码对象不能只改 SQLAlchemy model，至少要同步检查并修改：

- `ConversationTaskContextRecord`、`ConversationRunRecord`、`TaskRecord` 及其 schema/version；
- `TaskResponse` 等 Task API response，以及 Run 读取/委派 summary 的 response mapping；
- context、Run、Task CRUD 的 create/update/clone/delete；
- `ConversationTaskContextService`、`RuntimeContextManager`、`TaskService`；
- Run command/service、workflow operations、所有 completed/failed/cancelled/restart 路径；
- fork、edit、reset、orphan recovery 和 delegation summary；
- `init_schema.py` 的 model 注册、表约束和 schema test；
- `ConversationTaskSnapshotService` 的持久化职责拆分，以及 projector/transport 的依赖注入。

`TaskModel.parent_run_id` 仅表示父任务/分叉关系，不能用来代替新增的
`TaskModel.current_run_id`。

## 7. Fork、Edit 与一致性边界

### 7.1 Fork

fork 用例只 clone：

- TaskModel 的 Task/fork 元数据；
- ConversationRunModel 的历史 Run 前缀；
- ConversationTaskContextModel 的历史 context rows 及其 Transport metadata。

目标 Task 的 `current_run_id` 重置为 null；目标 Task 的 context usage/window 按产品规则
复制或重新计算，但规则必须落在 TaskModel。不得复制 snapshot，因为目标视图可由三模型
重建。system prompt/version 也必须按目标 Task 的规则复制或生成。所有 tool_call_id、message
id 和跨 message 引用的处理必须在 clone service 中集中定义并测试；checkpoint_thread_id
不能沿用源 Run。

### 7.2 Edit / reset

编辑导致的历史裁剪、Run 重置和 context 删除必须由同一个 Task operation 串行化，并在
service 事务中完成。删除 context rows 后不能留下引用它们的 tool part metadata；不得再
额外更新 snapshot 表。失败时由事务回滚，读路径从三个模型重新构建。

### 7.3 最终一致性

context、Run 和 Task 之间不追求一个覆盖流式 delta 的全局事务：

- 完整 message、Tool settle、Run terminal transition 是持久化边界；
- 每个持久化边界都先提交数据库事实，再更新内存 snapshot；两者之间不需要事务；
- 流式 chunk 只在内存；
- snapshot 是读模型，可在短时间内落后于 context；
- snapshot 读取时以 RunModel 的 status/end_reason 和 TaskModel 的 current_run_id/
  usage 校正；
- 任何“校正”不得反向写 snapshot 或篡改 context message。

## 8. 性能、日志和故障处理

取消每个 chunk 的整行 `state_json` UPDATE 后，SQLite 写入将降为 message/tool/Run 边界；
仍需保留 task operation 串行化、短事务、busy timeout 和结构化日志。不能承诺“绝不再
database locked”，应通过并发测试验证锁等待和重试是否足够。

关键日志至少包含：

- `state_rebuild_started` / `state_rebuild_completed` / `state_rebuild_failed`；
- `context_message_persisted`；
- `tool_observation_persisted`；
- `run_status_persisted`；

日志只记录 id、数量、耗时、schema version 和受控错误分类，不记录 token、密码、完整 prompt
或大段模型/工具正文。日志失败不得阻断 Agent。

## 9. 验收标准

### 9.1 数据与重建

- 新 schema 中不存在 `conversation_task_snapshots` 表，也不存在对其的业务读写；
- 任意 Task 的 snapshot 和 Agent context 都能只使用 TaskModel、ConversationRunModel、
  ConversationTaskContextModel 重建；
- 重建结果通过现有 `validate_snapshot`；
- status/end_reason/current_run_id/usage 与三个模型逐字段一致；
- 多 AI message、多 tool call、工具失败/取消、编辑、fork、重启和空 Task 均有 fixture；
- 目标 schema 的所有 context row 都包含重建所需 metadata；metadata 缺失时测试必须断言
  明确失败，而不是降级为猜测结果。

### 9.2 实时传输

- SSE 首帧仍是注册订阅者与读状态的原子操作；
- 流式 chunk 不触发 snapshot 全量数据库写；
- HTTP/SSE 断开不会取消或重放业务 Run；
- projector 异常有结构化日志且不杀死 Agent；
- 前端 wire schema、converter 和工具 renderer 无需建立第二套状态机。

### 9.3 并发和恢复

- 多 Task 同时流式、同 Task 互斥操作、tool 超时/取消均通过集成测试；
- 后端崩溃后 active Run 有限收敛，旧进程不能覆盖新 generation；
- 重启后不会隐式重放旧 workflow；
- 新数据库初始化可重复，schema 检查失败时不会启动业务 Run；
- LangGraph checkpoint 只在显式 continuation 流程被读取，不参与冷启动 context/snapshot 重建；
- 运行日志和 SQLite 日志仍可用于定位启动、锁等待、重建和恢复问题。

## 10. 实施顺序

1. 为 Task/Run/context 增加目标 schema 字段，补齐 typed schema 与序列化测试。
2. 把 system prompt、初始 user message、完整 AI message、ToolMessage 及 Transport metadata
   收敛到 context service 写路径。
3. 实现无副作用的三模型重建器和 context loader。
4. 为正常 Run、多个 AI step、工具成功/失败/取消、edit、fork、重启建立全新 fixture，直接
   验证三模型重建结果。
5. 切换 GET、SSE 首帧、attach/resume、fork、edit 的读路径。
6. 移除 snapshot upsert、snapshot clone 和相关 projector 持久化副作用。
7. 从新 schema 移除 snapshot 表及无用 CRUD。
8. 执行验收矩阵和并发/重启测试；不添加旧数据兼容 fallback。

## 11. 可执行任务拆分（TDD）

以下任务按顺序执行。每个任务必须先新增一个能证明目标行为缺失的失败测试，确认失败原因
是生产能力尚未实现，再写最小实现，最后运行相关回归测试。任务之间共享接口时，以前一项
已经通过的测试和目标 schema 为输入；不得绕过失败测试直接改生产代码。

### Task 1 — 三类事实模型与序列化契约

为 `TaskModel`、`ConversationRunModel`、`ConversationTaskContextModel` 及对应 Record/CRUD
建立目标字段和 typed serialization。覆盖 `current_run_id`、`context_window_total`、Run
usage/error、Context row id、Transport metadata、SystemMessage 持久化，以及新 schema 不
创建 `conversation_task_snapshots`。先写模型/record/schema 失败测试，再实现；不修改 HTTP
读路径和 workflow 行为。

### Task 2 — 三模型重建器与 Agent context loader

实现无副作用的 `ConversationTaskStateRebuilder` 和 context loader。只接受一个 Task record、
该 Task 的 Run records、Context records，不读取 snapshot、checkpoint、ToolRegistry 或内存
state。覆盖多 AI row 合并、`str(ConversationTaskContextModel.id)` message id、tool result
回填、缺失 metadata 显式失败、usage/current run/error 映射和 `validate_snapshot`。先写失败
单测，再实现纯函数；不切换线上读写路径。

### Task 3 — canonical message/tool 写路径

将初始 user message、完整 AI message、ToolMessage 和 Transport metadata 收敛到
`ConversationTaskContextService`。保证数据库事实成功提交后才更新内存 Transport state；不
在二者之间建立事务。覆盖 AI part 顺序、工具 presentation/display_data/status/error、幂等
tool settle、完整 AIMessage 字段保留，以及 Run 终态一次性持久化 usage/error/final_output。
先写会观察到旧双写/错误顺序的失败测试，再修改 runtime/context/tool/run service。

### Task 4 — 移除持久化 snapshot 并切换生命周期路径

删除 snapshot model/CRUD/初始化注册和所有 state_json upsert/clone/delete；将 GET、SSE 首帧、
attach、fork、edit、reset、orphan recovery 切换到“三模型重建 + 内存 Transport state”。
保留 attach 与 business resume 语义差异；checkpoint 只参与显式 workflow continuation，
不参与重建。先写失败的 schema、冷读、SSE 顺序、fork/edit/restart 和重启恢复测试，再完成
删除和切换。

### Task 5 — 全量验收与并发回归

基于目标 schema 建立正常 Run、多 AI step、多工具、工具失败/取消、edit、fork、后端重启和
空 Task fixture。验证三模型重建、数据库优先时序、前端既有 wire schema、同 Task 互斥及多
Task 并发。该任务只补验收测试和必要的生产修复，不引入兼容路径或第二事实源。

## 12. 需要产品/实现确认的唯一选项

1. 新生成的 context 是否要求 UI part/display_data 完全可重建；本方案默认要求，缺失即失败。
2. Run usage/error 使用显式 JSON 列还是 typed fields；两者都必须归 ConversationRunModel 所有。
3. system prompt 是否按 Task 固定，还是按提示词版本生成多条可追溯的 SystemMessage。

除上述选项外，事实所有权不再开放：Task 级归 TaskModel，Run 级归 ConversationRunModel，
message/tool 级归 ConversationTaskContextModel；snapshot 和 Agent runtime context 均从这三者
派生。
