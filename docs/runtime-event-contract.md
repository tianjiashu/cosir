# Runtime Event Contract

> 当前文档是运行时事件契约的事实快照，基于 CodeGraph 对后端 emit 点与前端消费点的调研整理。它用于前后端协作、人类介入开发和后续生成机器 Schema 的准备工作。

## 1. 文档目标

运行时事件是后端 Agent Runtime 与桌面客户端之间的核心协议。后端通过 SSE 持续发送 `RuntimeEvent`，前端据此渲染对话流、工具执行状态、任务终态和 trace 信息。

本文回答四个问题：

- 后端当前定义了哪些 `event_type`。
- 后端当前真实 emit 了哪些事件，以及 payload 是什么。
- 前端当前如何消费这些事件。
- 当前契约漂移和后续 Schema 化应该怎么处理。

本文不替代机器校验。长期目标是把后端事件模型升级为强类型 Schema，再生成 TypeScript 类型。

## 2. 通用事件信封

后端事件值对象定义在：

```text
apps/backend/app/models/runtime_event.py
```

当前 `RuntimeEvent.to_dict()` 输出结构：

```json
{
  "event_id": "uuid",
  "event_type": "run_started",
  "task_id": "task-id",
  "turn_id": "turn-id-or-null",
  "sequence": 0,
  "message_id": null,
  "tool_call_id": null,
  "created_at": "2026-07-23T00:00:00+00:00",
  "payload": {}
}
```

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `event_id` | `string` | 事件唯一 ID，用于前端幂等去重。 |
| `event_type` | `string` | 稳定事件类型，来自后端 `EventType` 枚举。 |
| `task_id` | `string` | 事件所属 task。 |
| `turn_id` | `string | null` | 事件所属 turn；正常 turn 级 SSE 事件应带值。 |
| `sequence` | `number` | 当前运行流内递增排序号；部分 runtime 外层事件默认为 `0`。 |
| `message_id` | `string | null` | 预留消息 ID，目前未见真实写入。 |
| `tool_call_id` | `string | null` | 工具调用 ID；`runner._record()` 会从 payload 的 `tool_call_id` 提升到顶层。 |
| `created_at` | `string` | UTC ISO 时间戳。 |
| `payload` | `object` | 随 `event_type` 变化的事件载荷。 |

补充约定：

- `event_id` 是前端去重的事实键。
- `sequence` 用于同一 task / turn 内排序，但当前外层 `runner._record()` 事件未显式分配递增序号。
- 后端 `RuntimeEvent` docstring 里已有通用展示信封字段建议：`display_format`、`component_type`、`title`、`summary`、`details`、`arguments`、`_ext`。当前真实 emit 大多未使用这些字段。

## 3. 后端事件类型定义

后端枚举定义在：

```text
apps/backend/app/models/enums/event_type.py
```

当前枚举成员：

| event_type | 当前状态 | 说明 |
| --- | --- | --- |
| `run_started` | 已真实 emit | turn 被认领并开始执行。 |
| `run_failed` | 已真实 emit | 运行失败。 |
| `run_cancelled` | 已真实 emit | 运行被取消。 |
| `run_finished` | 已真实 emit | 运行正常完成。 |
| `step_started` | 已真实 emit | ReAct 模型步骤开始。 |
| `model_requested` | 已真实 emit | 即将请求模型。 |
| `model_output_delta` | 已真实 emit | 模型回答文本增量。 |
| `model_thinking_delta` | 已真实 emit | 模型 reasoning / thinking 增量。 |
| `model_completed` | 已真实 emit | 单次模型输出完成。 |
| `model_failed` | 已定义，未发现 emit | 模型失败预留。 |
| `tool_call_requested` | 已定义，未发现 emit | 工具请求预留；当前模型工具调用只出现在 `model_completed.payload.tool_calls`。 |
| `tool_call_started` | 已定义，未发现 emit | 工具开始预留。 |
| `tool_call_finished` | 已真实 emit | 单个工具调用完成。 |
| `observation_added` | 已定义，未发现 emit | 工具 observation 回填预留。 |
| `final_response` | 已真实 emit | 最终回答文本。 |
| `human_input_requested` | 已定义，未发现 emit | Human-in-the-loop 预留。 |
| `human_input_received` | 已定义，未发现 emit | Human-in-the-loop 预留。 |

## 4. 后端 emit 点总览

CodeGraph 与精确搜索确认，当前后端真实 emit 点集中在四处：

| 文件 | 位置 | 责任 |
| --- | --- | --- |
| `apps/backend/app/core/runtime/runner.py` | `cancel_turn()` | 主动取消 turn，创建 `run_cancelled`。 |
| `apps/backend/app/core/runtime/runner.py` | `run_turn()` | 外层运行生命周期：`run_started`、部分 `run_failed`。 |
| `apps/backend/app/core/workflows/react/workflow.py` | `ReactLikeWorkflow.run()` | 将 LangGraph `custom/messages` 流转换为 `RuntimeEvent`。 |
| `apps/backend/app/core/workflows/react/nodes.py` | `_model_node()` / `_tools_node()` | 写入 ReAct 节点内的业务事件。 |
| `apps/backend/app/service/tool_execution/tool_execution_service.py` | `run_calls_with_events()` | 工具调用完成事件。 |

当前未发现其他后端业务代码直接构造或 emit `RuntimeEvent`。

## 5. 事件生命周期

当前默认 ReAct-like workflow 的常见成功路径：

```text
run_started
  -> step_started
  -> model_requested
  -> model_output_delta*
  -> model_thinking_delta*
  -> model_completed
  -> final_response
  -> run_finished
```

带工具调用的路径：

```text
run_started
  -> step_started
  -> model_requested
  -> model_output_delta*
  -> model_thinking_delta*
  -> model_completed
  -> tool_call_finished*
  -> step_started
  -> model_requested
  -> ...
  -> final_response
  -> run_finished
```

失败路径：

```text
run_started
  -> ...
  -> run_failed
```

取消路径：

```text
run_cancelled
```

或运行中由模型节点检测到取消：

```text
run_started
  -> step_started
  -> model_requested
  -> run_cancelled
```

## 6. 事件契约明细

### 6.1 `run_started`

后端 emit：

- `AgentRuntime.run_turn()` 在成功 claim pending turn 后 emit。

payload：

```json
{
  "status": "running",
  "agent": {}
}
```

信封：

- `turn_id`：有值，由 `_turn_id` 从 payload 提升。
- `sequence`：当前为默认 `0`。

前端消费：

- `useSSE.runtimeStatusFromEvent()` 将 task `execution_status` 置为 `active`，turn `status` 置为 `running`。
- `eventStore.appendEvent()` 存储并按 `event_id` 去重。
- `timelineProjector` 不渲染该事件。

### 6.2 `step_started`

后端 emit：

- `_model_node()` 每个模型步骤开始时 emit。

payload：

```json
{
  "step_id": "step-1",
  "kind": "model",
  "index": 1
}
```

前端消费：

- `eventStore` 保存。
- `timelineProjector` 不渲染，但会 flush 正在聚合的文本块。

当前漂移：

- `apps/shared/ts/events.ts` 当前写的是 `step_type` / `step_index`，与后端真实字段 `step_id` / `kind` / `index` 不一致。

### 6.3 `model_requested`

后端 emit：

- `_model_node()` 在请求模型前 emit。

payload：

```json
{
  "step_id": "step-1",
  "message_count": 3
}
```

前端消费：

- `eventStore` 保存。
- 当前 `apps/shared/ts/events.ts` 未声明该事件类型。
- `timelineProjector` 不渲染。

当前漂移：

- 后端真实 emit，前端共享类型缺失。

### 6.4 `model_output_delta`

后端 emit：

- `ReactLikeWorkflow.run()` 从 LangGraph `messages` stream 中抽取文本增量后 emit。

payload：

```json
{
  "step_id": "step-1",
  "text": "增量文本"
}
```

前端消费：

- `timelineProjector` 将相邻 `model_output_delta` 聚合为 assistant 消息。

当前漂移：

- 前端 payload 类型只写 `text`，未写 `step_id`。

### 6.5 `model_thinking_delta`

后端 emit：

- `_model_node()` 从模型 chunk 中抽取 reasoning 内容后 emit。

payload：

```json
{
  "step_id": "step-1",
  "text": "思考增量"
}
```

前端消费：

- `timelineProjector` 将相邻 `model_thinking_delta` 聚合为 thinking 消息。

当前漂移：

- 前端 payload 类型只写 `text`，未写 `step_id`。

### 6.6 `model_completed`

后端 emit：

- `_model_node()` 完成一次模型输出合并后 emit。

payload：

```json
{
  "step_id": "step-1",
  "text": "本次模型输出完整文本",
  "tool_calls": []
}
```

`tool_calls` 由内部 `ToolCall` 值对象经 `asdict()` 序列化，常见字段包括：

```json
{
  "tool_name": "read_file",
  "arguments": {},
  "call_id": "tool-call-id"
}
```

前端消费：

- `eventStore` 保存。
- 当前 `apps/shared/ts/events.ts` 未声明该事件类型。
- `timelineProjector` 不渲染。

当前漂移：

- 后端真实 emit，前端共享类型缺失。

### 6.7 `tool_call_finished`

后端 emit：

- `ToolExecutionService.run_calls_with_events()` 每个工具调用执行完成后 emit。

payload：

```json
{
  "step_id": "step-1",
  "tool_name": "read_file",
  "status": "success",
  "tool_call_id": "tool-call-id"
}
```

信封：

- `ReactLikeWorkflow.run()` 转发 custom event 时没有把 `payload.tool_call_id` 提升到顶层；顶层 `tool_call_id` 当前只有 `runner._record()` 路径会提取。

前端消费：

- `timelineProjector.projectTool()` 渲染工具项。
- 当前前端仅当 `payload.status === "error"` 时显示 error 状态，否则显示 completed。

当前漂移：

- 前端 payload 类型未声明 `step_id` / `tool_call_id`。
- 前端类型允许 `error`，但后端当前 emit 中未携带 `error` 字段；错误内容可能只在工具 observation 或日志中。

### 6.8 `final_response`

后端 emit：

- `_model_node()` 在无工具调用且有最终文本时 emit。

payload：

```json
{
  "text": "最终回答",
  "step_id": "step-1",
  "status": "completed"
}
```

后端副作用：

- turn 状态更新为 `completed`。
- turn `response_text` 落库，用于历史回看。

前端消费：

- `useSSE.runtimeStatusFromEvent()` 当前把 `final_response` 当作 completed 终态，同步 task/turn 状态，并清空 streaming turn。
- `timelineProjector` 当前不直接渲染 `final_response`，主要依赖 `model_output_delta` 聚合展示回答，或历史 `turn.response_text` fallback。

当前漂移：

- 前端 `FinalResponsePayload` 只声明 `status`，缺失 `text` / `step_id`。

### 6.9 `run_finished`

后端 emit：

- `_model_node()` 在最终回答后 emit。

payload：

```json
{
  "status": "completed",
  "step_id": "step-1"
}
```

前端消费：

- `useSSE.runtimeStatusFromEvent()` 同步 task/turn 为 completed。
- `timelineProjector` 渲染终态 status badge。

当前漂移：

- 前端 `RunFinishedPayload` 只声明 `status`，缺失 `step_id`。

### 6.10 `run_failed`

后端 emit：

来源一：`AgentRuntime.run_turn()` 发现 agent profile 不可用。

```json
{
  "status": "failed",
  "error": "agent_profile_unavailable",
  "requested_agent_id": "developer",
  "task_agent_id": "developer"
}
```

来源二：`AgentRuntime.run_turn()` 捕获异常。

```json
{
  "status": "failed",
  "error": "异常文本"
}
```

来源三：`_model_node()` 达到最大步数。

```json
{
  "status": "failed",
  "error": "max_steps_reached"
}
```

来源四：`_model_node()` 模型无有效输出。

```json
{
  "error": "invalid_model_output",
  "message": "Model did not return tool call or final text."
}
```

来源五：`_tools_node()` 连续工具错误达到上限。

```json
{
  "step_id": "step-1",
  "status": "failed",
  "error": "tool_error_limit_reached",
  "tool_name": "read_file"
}
```

前端消费：

- `useSSE.runtimeStatusFromEvent()` 同步 task/turn 为 failed，`end_reason` 取 `payload.error` 或默认 `run_failed`。
- `timelineProjector` 渲染终态 status badge。

当前漂移：

- 前端 `RunFailedPayload` 要求 `status: "failed"`，但后端 `invalid_model_output` 分支没有 `status`。
- 不同失败来源 payload 字段不一致，应 Schema 化为公共失败字段 + 可选上下文字段。

### 6.11 `run_cancelled`

后端 emit：

来源一：`AgentRuntime.cancel_turn()` 主动取消。

```json
{
  "status": "cancelled"
}
```

来源二：`_model_node()` 流式模型过程中检测到 turn 已取消。

```json
{
  "step_id": "step-1",
  "status": "cancelled",
  "error": "cancelled"
}
```

前端消费：

- `useSSE.runtimeStatusFromEvent()` 同步 task/turn 为 cancelled，`end_reason` 固定为 `run_cancelled`。
- `timelineProjector` 渲染终态 status badge。

当前漂移：

- 前端 `RunCancelledPayload` 只声明 `status`，缺失可选 `step_id` / `error`。

## 7. 已定义但当前未真实 emit 的事件

### 7.1 `model_failed`

后端枚举已定义，当前未发现 emit。

建议后续语义：

- 模型调用失败但 workflow 还可能包装为 `run_failed`。
- payload 至少包含 `step_id`、`error`、可选 `status: "failed"`。

### 7.2 `tool_call_requested`

后端枚举已定义，当前未发现 emit。

当前替代来源：

- 工具请求信息存在于 `model_completed.payload.tool_calls`。

建议后续语义：

- 若前端需要在工具审批前展示“模型请求工具”，应在 `_model_node()` 或 `_tools_node()` 显式 emit。

### 7.3 `tool_call_started`

后端枚举已定义，当前未发现 emit。

建议后续语义：

- `ToolExecutionService` 调用 scheduler 前 emit。
- payload 包含 `step_id`、`tool_name`、`tool_call_id`、`arguments`。

### 7.4 `observation_added`

后端枚举已定义，当前未发现 emit。

当前替代来源：

- 工具 observation 被转换成 `RuntimeMessage(role="tool")` 供下一步模型消费，但未单独 emit 到前端。

建议后续语义：

- 如果前端需要展示工具结果摘要，应 emit `observation_added`，payload 包含 `step_id`、`tool_name`、`tool_call_id`、`status`、`summary`、`details`。

### 7.5 `human_input_requested` / `human_input_received`

后端枚举已定义，当前未发现 emit。

当前注释说明：

- Human-in-the-loop 预留。
- 本轮仅定义、不 emit。

建议后续语义：

- 审批、澄清、用户确认等中断恢复流程统一走这两个事件。

## 8. 前端消费点总览

| 文件 | 当前职责 | 消费方式 |
| --- | --- | --- |
| `apps/desktop/src/services/sse.ts` | SSE 解析 | `JSON.parse(data)` 后直接断言为 `RuntimeEvent`，不做运行时 schema 校验。 |
| `apps/desktop/src/hooks/useSSE.ts` | 事件接入与状态同步 | 所有事件进入 `eventStore`；仅 `run_started`、`final_response`、`run_finished`、`run_failed`、`run_cancelled` 同步 task/turn 状态。 |
| `apps/desktop/src/stores/eventStore.ts` | 事件事实存储 | 按 `event_id` 去重，按 task/turn 聚合，按 `sequence` 和 `created_at` 排序。 |
| `apps/desktop/src/services/timeline/projector.ts` | 对话流投影 | 渲染 `model_output_delta`、`model_thinking_delta`、`tool_call_*`、`run_finished`、`run_failed`、`run_cancelled`。 |
| `apps/desktop/src/components/chat/StatusBadge.tsx` | 终态标签 | 只展示 `run_finished`、`run_failed`、`run_cancelled`。 |
| `apps/shared/ts/events.ts` | 前端共享类型 | 当前手写，已与后端枚举和 payload 发生漂移。 |

## 9. 当前已发现的契约漂移

### 9.1 后端事件类型多于前端类型

后端已定义但前端 `RuntimeEventType` 缺失：

- `model_requested`
- `model_completed`
- `model_failed`
- `human_input_requested`
- `human_input_received`

### 9.2 后端真实 emit 但前端类型缺失

- `model_requested`
- `model_completed`

这两个事件目前会被 SSE parser 接收并进入 `eventStore`，但 TypeScript 类型不承认它们。

### 9.3 payload 字段不一致

| event_type | 后端真实 payload | 前端当前类型 |
| --- | --- | --- |
| `step_started` | `step_id` / `kind` / `index` | `step_type` / `step_index` |
| `model_output_delta` | `step_id` / `text` | `text` |
| `model_thinking_delta` | `step_id` / `text` | `text` |
| `final_response` | `text` / `step_id` / `status` | `status` |
| `run_finished` | `status` / `step_id` | `status` |
| `run_cancelled` | `status`，有时含 `step_id` / `error` | `status` |
| `tool_call_finished` | `step_id` / `tool_name` / `status` / `tool_call_id` | `tool_name` / `status` / 可选 `error` |
| `run_failed` | 多分支 payload，部分无 `status` | `status` / `error` |

### 9.4 顶层 `tool_call_id` 不稳定

`runner._record()` 会把 payload 里的 `tool_call_id` 提升到顶层。但 workflow custom event 转 `RuntimeEvent` 时没有做同样提升，所以 `tool_call_finished` 的顶层 `tool_call_id` 当前可能为 `null`，真实 ID 在 `payload.tool_call_id`。

### 9.5 `sequence` 语义不完全统一

`ReactLikeWorkflow.run()` 内部递增 `sequence`。`runner._record()` 创建的外层事件当前使用默认 `sequence=0`。这会影响前端排序稳定性，尤其是 `run_started` 与其他 `sequence=0` 事件同时存在时。

## 10. 建议的事件分类

为了后续 Schema 化和前端消费清晰，建议把事件分为五类。

### 10.1 Run 生命周期事件

- `run_started`
- `run_finished`
- `run_failed`
- `run_cancelled`

用途：

- 更新 task / turn 执行状态。
- 渲染终态。
- 控制 streaming turn 生命周期。

### 10.2 Step 生命周期事件

- `step_started`

用途：

- 标识 ReAct step。
- 后续可用于 trace、折叠展示、性能统计。

### 10.3 Model 事件

- `model_requested`
- `model_output_delta`
- `model_thinking_delta`
- `model_completed`
- `model_failed`

用途：

- 展示模型输出。
- 展示 thinking。
- 调试模型请求与结果。
- 关联 tool calls。

### 10.4 Tool 事件

- `tool_call_requested`
- `tool_call_started`
- `tool_call_finished`
- `observation_added`

用途：

- 展示工具生命周期。
- 支持审批前/执行中/执行后状态。
- 展示工具结果摘要。

### 10.5 Human-in-the-loop 事件

- `human_input_requested`
- `human_input_received`

用途：

- 用户审批、确认、澄清、恢复中断。

## 11. 后续 Schema 化建议

### 11.1 第一阶段：修正文档与 shared TS

先将 `apps/shared/ts/events.ts` 修成与后端当前事实一致：

- `RuntimeEventType` 补齐后端枚举。
- 增加 `RuntimeEventPayloadMap`。
- 将 `RuntimeEvent` 改成按 `event_type` 判别的 union。
- 对已定义未 emit 的事件，也要显式建 payload 类型，但标注为预留。

目标：

```ts
export interface RuntimeEventPayloadMap {
  run_started: RunStartedPayload;
  step_started: StepStartedPayload;
  model_requested: ModelRequestedPayload;
  model_output_delta: ModelOutputDeltaPayload;
  model_thinking_delta: ModelThinkingDeltaPayload;
  model_completed: ModelCompletedPayload;
  model_failed: ModelFailedPayload;
  tool_call_requested: ToolCallRequestedPayload;
  tool_call_started: ToolCallStartedPayload;
  tool_call_finished: ToolCallFinishedPayload;
  observation_added: ObservationAddedPayload;
  final_response: FinalResponsePayload;
  run_finished: RunFinishedPayload;
  run_failed: RunFailedPayload;
  run_cancelled: RunCancelledPayload;
  human_input_requested: HumanInputRequestedPayload;
  human_input_received: HumanInputReceivedPayload;
}

export type RuntimeEventType = keyof RuntimeEventPayloadMap;
```

### 11.2 第二阶段：后端 Pydantic 事件响应模型

新增后端响应 schema：

```text
apps/backend/app/api/schemas/response/runtime_event_response.py
apps/backend/app/api/schemas/response/runtime_event_payload/*.py
```

将当前 dataclass `RuntimeEvent` 的 HTTP/SSE 输出映射到 Pydantic response model。

注意：

- 不一定马上替换后端内部 dataclass。
- 可以先在 API 边界做 response schema。
- payload 应按 `event_type` 做判别联合。

### 11.3 第三阶段：生成 JSON Schema 和 TS 类型

建议产物：

```text
packages/shared/schema/runtime-events.schema.json
packages/shared/ts/generated/runtime-events.ts
```

建议脚本：

```text
scripts/generate_runtime_event_schema.py
```

建议检查：

```bash
python scripts/generate_runtime_event_schema.py
git diff --exit-code packages/shared/schema packages/shared/ts/generated
```

目标：

- 后端 Pydantic 是机器事实源。
- 前端 TS 类型由 Schema 生成。
- 人类文档解释语义与消费行为。

## 12. 短期修复清单

建议按顺序处理：

1. 修正 `apps/shared/ts/events.ts` 与后端真实枚举/payload 对齐。
2. 给 `model_requested`、`model_completed` 增加前端类型，即使暂时不渲染。
3. 修正 `step_started` payload 类型。
4. 修正 `final_response` / `run_finished` / `run_cancelled` payload 类型。
5. 明确 `run_failed.status` 是否必填；如果要求必填，后端 `invalid_model_output` 分支需要补 `status: "failed"`。
6. 明确 `tool_call_id` 应该稳定在顶层、payload，还是两处都保留。
7. 明确是否要真实 emit `tool_call_requested` / `tool_call_started` / `observation_added`。
8. 明确 `sequence` 是否应由 runtime 外层统一分配，而不是 workflow 局部分配。

## 13. 调研方法记录

本次调研使用 CodeGraph 优先定位：

```bash
codegraph explore "RuntimeEvent EventType runtime_event event_type SSE events payload run_started run_finished run_failed model_output_delta tool_call_requested backend frontend"
codegraph explore "EventType write_event RuntimeEvent payload _record run_turn cancel_turn _model_node _tools_node run_calls_with_events all backend emit points"
codegraph explore "RuntimeEvent frontend consumers useSSE syncRuntimeStatus eventStore projectTurnTimeline projectEntries projectTool StatusBadge"
```

随后用精确搜索补齐：

```bash
rg -n "EventType\\." apps/backend/app apps/backend/tests
rg -n "RuntimeEvent\\(" apps/backend/app apps/backend/tests
rg -n "event_type\\s*(===|==|!==|!=)|RuntimeEventType|model_requested|model_completed|model_failed|human_input" apps/desktop/src apps/shared/ts apps/desktop/src/tests
```

结论：当前后端真实 emit 点没有散落到其他模块；前端消费集中在 SSE parser、`useSSE`、`eventStore`、timeline projector 和 status badge。
