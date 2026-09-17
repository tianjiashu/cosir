# 后端 Tool UI 执行生命周期改造方案（审查修订版）

## 结论

当前后端方案不能直接通过验收，但不需要推翻现有 Conversation 重构。现有代码已经具备 canonical conversation、`conversation_tool_calls`、revision 订阅和 Assistant Transport state 快照；需要补齐的是工具调用的生命周期事实、稳定的 message-part 关联和异常/审批/取消收口。

本方案修订后可行，前提是按本文件完成后端改造。核心原则是：后端只产生 canonical conversation state，前端通过 Assistant Transport 渲染；后端不引入 assistant-ui 类型、不返回 HTML，也不恢复旧 SSE/eventStore。

## 一、代码事实审查

### 已具备的能力

- `ConversationMutationWriter` 已能原子创建/完成 `conversation_tool_calls`，并写入 conversation revision。
- `ConversationToolCallModel` 已有 `task_id`、`turn_id`、`message_id`、`part_id`、`tool_call_id`、`tool_name`、`args_json`、`result_json`、`status`、`error_text`。
- `ConversationStateService.build_messages()` 已把工具调用投影成中性 `tool-call` part，并能投影 args/result/error/status。
- `ConversationRunSubscriptionService` 以 revision 作为唤醒索引，每次从 canonical facts 重建完整快照，具备断线后按 run 续订的基础。
- `/assistant`、`/tasks/{task_id}/assistant/state`、`/tasks/{task_id}/runs/{run_id}/assistant/stream` 和 `/runs/{run_id}/cancel` 已形成新传输链路的雏形。
- `ToolDefinition.display` 已提供 verb、icon、expandable、expand_layout 等展示提示；工具自身仍保持后端执行职责。

### 当前不能满足需求的地方

1. **执行前时机不可靠**

   `model_node` 先把 LangChain `AIMessage` 交给 `RuntimeContextManager`，工具事实通常由 `tools_node`/`RuntimeOperations` 的 `on_tool_call_started` 才创建。这样前端只能在工具节点开始后看到调用，无法保证“handler 执行前已有可展示的工具卡片”。

2. **状态机不完整**

   `ToolObservation` 是执行结果值对象，状态主要是 success/error/cancelled；它不能替代 UI 所需的 pending/running/requires-action/terminal 生命周期。`conversation_tool_calls.status` 虽默认 pending，但当前写入和状态转移没有被设计成完整、受约束的状态机。

3. **tool part 不是稳定的一等关联**

   `ConversationToolCallModel` 有 `part_id` 字段，但当前投影是遍历 message parts 后再追加 tool calls；工具调用的 UI 顺序不由 message part 的 sequence 决定。运行时恢复还存在按 turn 内位置推断 tool message 对应 `tool_call_id` 的逻辑，这对并行工具和异常重试不够可靠。

4. **结果数据有两条语义，容易投影错源**

   `ToolObservation.data` 是客户端结构化数据，但工具观察写回模型上下文前会刻意清理客户端字段。Transport 必须从 canonical tool-call result 读取展示数据，不能从已经裁剪过的模型 ToolMessage 反推。

5. **异常和取消存在悬挂风险**

   工具执行、回调、进程隔离或运行器异常时，已创建但未完成的工具调用必须统一进入 failed/cancelled；否则前端会永久显示 running，恢复后也无法判断是否需要重放。批量并行调用必须逐个收口，不能因为其中一个失败而遗留其他调用。

6. **审批尚未闭合为 Assistant UI action**

   LangGraph `interrupt()`/resume 是服务端编排能力；Assistant UI 的工具状态需要 `requires-action` 和可恢复的用户动作。当前不能仅靠把 status 字符串改成 requires-action 就完成审批，必须提供 command/resume 的传输映射，并保证恢复不会重复创建或重复执行工具。

7. **现有 Transport 只在 revision 前进时发送快照**

   这条机制本身可用，但要求每个生命周期变更都先提交 canonical mutation，再由同一个 `toolCallId` 在快照中更新。不能只写日志或只修改内存状态。

## 二、修订后的后端目标契约

### 1. canonical 工具调用事实

`conversation_tool_calls` 作为工具调用事实唯一来源，建议明确以下状态：

```text
pending → running → completed
                  ↘ failed
                  ↘ cancelled
pending → requires_action → pending/running
pending/running → failed/cancelled
```

要求：

- `pending` 表示模型已产生并已持久化调用，但 handler 尚未执行。
- `requires_action` 表示等待服务端审批恢复，不表示失败。
- `running` 表示已通过审批且 handler 已开始。
- `completed`、`failed`、`cancelled` 是终态；终态不可被普通完成回调覆盖。
- 所有状态转移由 `ConversationMutationWriter` 提供条件更新和 revision；禁止在 node、handler 或 API 中直接改 ORM。
- `tool_call_id` 在 task 内唯一；重复命令必须幂等，参数不一致必须返回冲突。
- 结果、错误、截断标识、retryable、开始/结束时间和必要的展示元数据应归入 canonical 记录或其结构化 data，不放进日志作为事实。

### 2. 稳定的 message-part 关联

模型决定调用后，后端应在同一事务中：

1. 找到本轮 assistant message；
2. 按模型返回顺序创建一个 `tool-call` message part；
3. 将 `conversation_tool_calls.part_id` 指向该 part；
4. 创建 status=pending 的工具调用事实；
5. 提交一次 revision。

如果现有 `ConversationMessagePartModel.data_json` 足以保存中性工具字段，可继续复用它；否则只扩展中性 canonical schema。不要把 Assistant UI 的 `ToolCallMessagePart` 类型下沉到 service/storage。

`ConversationStateService` 应按 message part sequence 投影工具 part，并通过 `part_id`/tool-call 关联补充动态字段。禁止在投影阶段按创建时间把工具调用追加到消息末尾，也禁止按位置猜测 tool message 的 call id。

### 3. Assistant Transport state

后端 Transport 只输出中性 JSON state，至少保证每个 tool-call part 具有：

```json
{
  "type": "tool-call",
  "toolCallId": "call_xxx",
  "toolName": "read_file",
  "args": {"path": "..."},
  "status": "pending|running|requires-action|completed|failed|cancelled",
  "result": {},
  "error": "...",
  "isError": true
}
```

具体 wire 名称以当前 Assistant Transport converter 的实际契约为准；关键是不改变 `toolCallId`，而是在同一 part 上做状态/结果更新。若当前 converter 只接受 `running/completed/incomplete`，应在 API 适配层做显式映射：

- pending → running（或新增中性 pending 后由 converter 映射）；
- requires-action → requires-action，并携带审批信息；
- failed/cancelled → incomplete，并保留 error/cancelled 原因。

不要把后端内部的 `success` 直接暴露给 Assistant UI；它是观察结果状态，不是 UI 工具调用状态。

## 三、执行时序

```text
model output
  ↓
validate + create pending tool facts/parts (one commit)
  ↓ revision → Transport: tool card appears
approval required?
  ├─ yes: pending → requires-action → wait for resume
  └─ no:  pending → running → execute handler
                         ↓
              completed / failed / cancelled (one commit per call)
                         ↓ revision → same card shows result
```

具体实现边界：

- `model_node` 负责把合法模型工具调用交给运行时操作门面；不直接访问 storage。
- `RuntimeOperations` 暴露“创建 pending 调用”“状态转移”“完成调用”的领域端口。
- `tools_node` 负责审批编排、取消检查和调用执行，不负责拼装 Transport state。
- `ToolExecutionService` 的 started/finished 回调必须是幂等的；执行异常也必须进入 finished/failure 收口。
- `ConversationRunExecutor`/run-level finally 负责兜底扫描本 run 的非终态工具调用，避免进程级异常留下永久 pending/running。
- 只有 canonical mutation 成功后，订阅服务才向 `assistant-stream` 更新 state。

## 四、审批、取消、断线和重启

### 审批

- 服务端继续拥有权限、风险和审批决定；前端不能直接执行工具。
- `requires-action` 必须包含可供前端展示的安全摘要，以及服务端认可的 approval request id。
- resume 请求必须绑定 task、run、approval request 和 revision/状态，重复批准或拒绝必须幂等。
- resume 后从 checkpoint 的中断点继续，不能重新走一遍模型工具选择，也不能重复插入 tool-call fact。

### 取消

- 先原子写 run cancelled，再向执行器发协作取消信号；已有实现方向可保留。
- pending/requires-action 调用直接 cancelled；running 调用由执行器尽力终止，最终必须由执行回调或 run finally 收口。
- 取消后的 late success 不得覆盖 cancelled。
- 每一个已持久化 call 都必须有对应的模型上下文 ToolMessage 占位或结果，保证下一次模型请求不会出现悬空 tool_calls。

### 断线、恢复、重启

- 客户端断线不等于业务取消；后台 run 继续执行。
- resume endpoint 只按 task/run/revision 订阅 canonical state，不重新驱动 Agent。
- 首帧返回当前完整快照，后续只在 revision 前进时更新；重连后重新读取事实而不是依赖内存事件。
- 进程重启恢复时，扫描 run 的 pending/running 调用：根据执行租约/attempt 决定恢复、取消或失败，禁止无条件重放具有副作用的工具。

## 五、不同工具的结果展示数据约定

后端不负责渲染组件，但要提供稳定、受预算限制的结构化 result 和 `ToolDefinition.display` 提示：

| 工具类别 | result 建议 | display/安全要求 |
| --- | --- | --- |
| `list_directory`、`search_content`、`find_files` | 条目数组、总数、截断信息 | `list`/details；限制条目数和字段 |
| `read_file`、`web_extract` | 内容、路径/来源、截断信息 | details；脱敏并限制大小 |
| `write_file`、`patch`、`apply_patch`、`delete` | 文件、变更摘要、diff/快照标识 | `diff`/write；写入前后均保留审计与撤销信息 |
| `execute_terminal` | exit code、stdout、stderr、duration、截断信息 | terminal；禁止把密钥写入 result/log |
| `web_search` | 来源列表、标题、URL、摘要 | list；URL 经过安全校验 |
| `delegate_task` | 子任务 id、状态、摘要、可恢复信息 | details；父子 run 关联明确 |

`result` 必须是可序列化、脱敏、预算受限的客户端数据；模型上下文继续使用受限的 `content/error/reason`，两者不能互相反推。

## 六、与 Assistant UI 官方能力的对应

官方 Tool UI 支持后端定义工具：前端注册工具名和 renderer，工具本身仍在后端执行；renderer 可根据 `args`、`result`、`status`、`toolCallId` 和 approval/resume 能力显示调用前、执行中和执行后状态。工具可以放在 `ToolGroup` 中，也可以作为 standalone tool UI。

因此后端只需稳定提供同一 tool-call part 的生命周期和结构化数据，不需要自研工具卡片协议或后端 UI 组件。官方状态语义中的 `running`、`requires-action`、`complete`、`incomplete` 应由 API converter 明确映射，而不是让前端猜测 `ToolObservation.status`。

参考官方文档：

- [Assistant UI Tool UI](https://www.assistant-ui.com/docs/tools/tool-ui)
- [Assistant Transport API](https://www.assistant-ui.com/docs/api-reference/transport/assistant-transport)
- [Tool status API](https://www.assistant-ui.com/docs/api-reference/tools/status)

## 七、必须修改的后端模块

### 必须做

- `ConversationMutationWriter`：增加 pending/create、requires-action、running、terminal 的条件状态转移；补齐幂等和终态保护。
- `ConversationToolCallCrud`/模型：保证状态、时间、attempt/lease、结构化 result/error 和 message-part 关联可查询；是否加列以实际 schema 评估。
- `model_node`/`RuntimeOperations`：模型产出合法工具调用后先持久化 pending fact/part，再进入工具执行节点。
- `tools_node`/`ToolExecutionService`：审批、开始、完成、异常、取消全部经过统一生命周期端口；每个 call 必须 terminalize。
- `ConversationStateService`：按 canonical part sequence 投影；同一 `toolCallId` 的更新只更新原 part；正确区分客户端 result 与模型观察消息。
- Assistant Transport API：把审批 resume、取消、重连所需的 run/approval/revision 校验纳入正式契约，并保持结构化错误响应。
- 测试：覆盖执行前快照、running 更新、成功/失败/取消、并行工具、审批恢复、断线重连、重复命令、运行器异常和重启恢复。

### 不应做

- 不在后端引入 `@assistant-ui/react` 类型或组件。
- 不恢复旧 SSE、`eventStore`、timeline projector 或客户端工具执行链路。
- 不把工具输出只写日志、不把 Transport state 存成第二事实源。
- 不用 `[object Object]` 字符串作为兜底结果；结构化数据必须在 API boundary 明确序列化，错误必须有稳定 code/message。

## 八、验收标准

1. 请求触发工具后，在任何 handler 真正执行前，前端已能收到同一 `toolCallId` 的 tool-call part。
2. 执行中至少收到 running 状态；完成后同一 part 显示结构化 result；失败/取消显示明确终态和安全错误。
3. 两个并行工具的顺序、结果和 `toolCallId` 不交叉；刷新或断线重连后状态一致。
4. 审批暂停显示 requires-action，批准/拒绝/重复操作均可恢复且不重复执行。
5. 取消后所有已创建工具调用最终可判定，late callback 不会覆盖 cancelled。
6. 任意工具执行异常、Transport 断开、进程重启都不会留下无法解释的永久 running。
7. 现有 Assistant Transport、canonical facts、checkpoint 和模型上下文协议测试全部通过。

## 最终审查判定

原后端方案：不通过，原因是生命周期时序、part 关联、终态收口和审批 Transport 契约尚未闭合。

本修订方案：架构方向可行，可进入实现；实现完成前不能宣称“工具执行前 UI 和执行后回显”已经可用。
