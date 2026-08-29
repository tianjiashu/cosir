# Assistant UI Transport Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将桌面端的对话输入、流式消息和工具/自定义事件展示迁移到 `@assistant-ui/react` 的 `useAssistantTransportRuntime`，同时保留现有 Task、Turn、LangGraph、工具、委派和检查点领域模型。

**Architecture:** 新增一组与旧 API 并存的 Assistant Transport 协议端点。端点通过官方 Python `assistant-stream` 同步 `AssistantTransportThreadState`；既有 `RuntimeEvent` 是状态投影器的输入，不直接改名后作为 message stream 输出。前端 converter 再将 state 转为 Assistant UI 的 message/data parts。前端 command 由适配层转译为现有 Task/Turn 创建、取消和恢复操作；`core/`、`tools/` 与存储模型不依赖 Assistant UI。

**Tech Stack:** Tauri 2、React 18、TypeScript 5.6、`@assistant-ui/react@0.15.17`、FastAPI、Python 3.11、LangGraph、pytest、Vitest。

**Spec:** `docs/plan/assistant-ui-migration-plan.md`（本计划以当前工作区代码复核后细化其实施顺序）。

## Global Constraints

- `TaskRecord`、`TurnRecord`、`RuntimeEvent`、27 种 payload、LangGraph workflow、工具系统和 SQLite 表均为领域事实源，首期不得重写或替换。
- `useAssistantTransportRuntime` 必须显式设置 `protocol: "assistant-transport"`；不得把它误配成只面向 message part 的 `data-stream` 协议。
- 后端先用官方 `assistant-stream` Python 包完成最小互操作 Spike，并将确定的版本写入 `apps/backend/pyproject.toml` 与 `uv.lock`；不手写未经协议测试的 `aui-state` wire format。
- 新 API 与现有 `POST /tasks/{task_id}/turns`、`GET /turns/{turn_id}/stream` 并存；迁移完成并验证后才讨论删除旧链路。
- 后端适配只能位于 `api → service/core` 合法方向；`core/`、`tools/`、`storage/` 不得 import Assistant UI 协议模块。
- 首期不做消息编辑、分支切换和 Sidebar 的 remote-thread-list；Assistant UI runtime 的 `edit` capability 保持关闭。
- 服务端执行工具，前端不得开启 `unstable_enableToolInvocations`，避免同一工具被执行两次。
- 文件、目录和 URL 附件必须保留 `AttachmentRef(kind, ref)` 语义；不能因 Assistant UI transport 原生只接受 text/image part 而静默丢失。
- 所有新增 Python 函数带完整中文四段式 docstring、类型注解，并通过 Ruff；共享 TS 契约由既有生成脚本生成，不手改生成物。
- 当前工作区已有大量未提交改动和既存 TypeScript 基线错误；每个任务只要求相关测试、相关 lint/类型检查不引入新错误，不以全仓 build 绿灯作为首期阻断条件。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `apps/backend/app/api/assistant_transport_api.py` | 适配端点：启动 run、读取 thread 快照、取消和恢复；只做 HTTP/依赖注入。 |
| `apps/backend/app/api/assistant_transport/state_projector.py` | 纯函数：从 Task/Turn/RuntimeEvent 构建和增量更新 `AssistantTransportThreadState`。 |
| `apps/backend/app/api/schemas/request/AssistantTransportRunRequest.py` | 解析并校验前端 command、workspace/task/model/附件上下文。 |
| `apps/backend/app/api/schemas/response/AssistantTransportStateResponse.py` | thread 历史快照及可恢复 run 的响应模型。 |
| `apps/backend/app/service/task/assistant_transport_service.py` | 将 command 映射到 Task/Turn/事件历史；不包含 HTTP/SSE 字符串拼接。 |
| `apps/backend/tests/test_assistant_transport_state_projector.py` | 覆盖所有运行时事件到 transport state 的投影和非法输入。 |
| `apps/backend/tests/test_assistant_transport_api.py` | 覆盖创建、已有 task、取消、恢复和旧端点不回归。 |
| `apps/desktop/src/services/assistantTransport.ts` | transport API 请求、初始 state 拉取和取消请求。 |
| `apps/desktop/src/services/assistantTransportState.ts` | 后端快照到 Assistant UI `ThreadMessage` 的纯转换；不读取 React store。 |
| `apps/desktop/src/components/chat/AssistantConversationRuntime.tsx` | 唯一 `AssistantRuntimeProvider` 与 `useAssistantTransportRuntime` 装配点。 |
| `apps/desktop/src/components/chat/AssistantDataParts.tsx` | delegation/file-change/context/tool-output 的 data renderers。 |
| `apps/desktop/src/components/layout/AssistantComposer.tsx` | 基于 `ComposerPrimitive` 的输入、附件和停止操作。 |
| `apps/desktop/src/components/layout/ChatPanel.tsx` | 迁移后只负责任务选择、滚动容器和新 Thread 视图边界。 |

## 领域与协议映射

| 现有事实 | Assistant UI 协议对象 | 首期处理 |
|---|---|---|
| `TaskRecord.task_id` | `threadId = "task:<id>"` | 字符串仅为 transport 外观 ID；数据库仍为 int。 |
| `TurnRecord.turn_id` | `runId = "turn:<id>"` | 一个 turn 对应一次 assistant run。 |
| `TurnRecord.input_text` | user message text part | 新 task/首 turn 由一个 transport 请求原子创建。 |
| `model_output_delta` | state 中 assistant text 字段增量 | converter 转为 text part。 |
| `model_thinking_delta` | state 中 reasoning 字段增量 | converter 转为 reasoning part，复用 ThinkingBlock 风格。 |
| `tool_call_*` | state 中 tool-call/result DTO | converter 转为 tool-call/data part；绝不前端执行工具。 |
| `delegation_*`、`file_change_*`、`tool_output_delta`、`context_usage` | state 中 custom entries | converter 转为 named data part，复用现有项目组件。 |
| `human_input_*` / LangGraph interrupt | state 中 approval entry + resume API | 先做 spike；未验证前不得切换主路径。 |

---

### Task 1: 先建立协议 Spike 与不可退化基线

**Files:**

- Create: `apps/backend/tests/test_assistant_transport_contract_spike.py`
- Create: `apps/desktop/src/tests/assistantTransport.contract.test.ts`
- Modify: `docs/plan/assistant-ui-migration-plan.md`（补充 spike 结论和实际协议样例）

**Interfaces:**

- 输入事实：`CreateTurnRequest(input_text, provider_id, model_name, reasoning_effort, attachments)`、`RuntimeEvent`、`AttachmentRef`。
- 输出事实：一份固定的 command 请求 JSON 与一份 `assistant-stream` state-operation 样例；不创建生产端点。

- [ ] **Step 1: 写后端失败测试，固定 text/reasoning/tool/custom-state 的最小 state 合约**

```python
def test_transport_state_contract_has_stable_custom_entry_names() -> None:
    state = apply_runtime_event(_empty_state(), _delegation_started_event())
    assert state["messages"][-1]["parts"][-1]["name"] == "delegation"
```

- [ ] **Step 2: 写前端失败测试，固定 command 裁剪和非图片附件外带规则**

```ts
expect(buildTransportRequest({
  commands: [addMessage("review this")],
  attachments: [{ kind: "file", ref: "H:/repo/a.ts" }],
})).toMatchObject({
  commands: [{ type: "add-message" }],
  attachments: [{ kind: "file", ref: "H:/repo/a.ts" }],
});
```

- [ ] **Step 3: 运行 Spike 测试，确认当前缺少 adapter 而失败**

Run:

```powershell
uv run pytest tests/test_assistant_transport_contract_spike.py -q
npm.cmd test -- --run src/tests/assistantTransport.contract.test.ts
```

Expected: 失败原因仅为尚未实现 state projector/request builder；现有 Task/Turn API 不被改动。

- [ ] **Step 4: 以最小独立函数实现 Spike，并明确审批落点**

实现只允许在测试辅助或临时模块中存在以下接口：

```python
def apply_runtime_event(
    state: AssistantTransportThreadState, event: RuntimeEvent
) -> AssistantTransportThreadState: ...
```

```ts
export function buildTransportRequest(input: TransportSendInput): Record<string, unknown>;
```

使用官方 `assistant-stream` 的 `create_run` / `DataStreamResponse` 让 state 变更发出协议操作；审批在 state 中统一表达为 `parts[].name == "approval"`，提交调用独立 resume endpoint，不尝试把服务端工具执行伪装为浏览器工具调用。

- [ ] **Step 5: 记录验收结论并删除 Spike 临时实现的重复代码**

把最终 JSON/state-operation 样例写入 `docs/plan/assistant-ui-migration-plan.md`，随后让 Task 2 的正式 state projector 成为唯一实现。若文件/目录/URL 附件无法通过自定义请求字段无损传递，停止迁移并先设计附件协议，不进入 Task 2。

---

### Task 2: 建立后端 Assistant Transport 契约和纯状态投影器

**Files:**

- Create: `apps/backend/app/api/assistant_transport/state_projector.py`
- Create: `apps/backend/app/api/schemas/request/AssistantTransportRunRequest.py`
- Create: `apps/backend/app/api/schemas/response/AssistantTransportStateResponse.py`
- Create: `apps/backend/tests/test_assistant_transport_state_projector.py`
- Modify: `apps/backend/app/api/schemas/__init__.py`
- Modify: `scripts/generate_api_ts.py`
- Modify: `apps/shared/ts/api.ts`（经生成脚本）

**Interfaces:**

- `AssistantTransportThreadState`：包含稳定 thread/run/message IDs、结构化 messages、custom entries、cursor、`is_running` 与 context usage 的 JSON 可序列化状态。
- `apply_runtime_event(state, event) -> AssistantTransportThreadState`：返回更新后的状态，不做 IO 或协议编码。
- `AssistantTransportRunRequest`：`workspace_id: int`、`task_id: int | None`、`commands: list[dict[str, Any]]`、`provider_id: int | None`、`model_name: str | None`、`reasoning_effort: str | None`、`attachments: list[AttachmentRef] | None`。
- `AssistantTransportStateResponse`：`thread_id: str`、`task_id: int`、`messages: list[dict[str, object]]`、`is_running: bool`、`active_run_id: str | None`。

- [ ] **Step 1: 写参数校验失败测试**

```python
def test_run_request_rejects_two_user_messages() -> None:
    with pytest.raises(ValidationError, match="exactly one add-message"):
        AssistantTransportRunRequest.model_validate(_request_with_two_messages())
```

同时覆盖：缺少 `workspace_id` 的新 thread、`provider_id/model_name` 非成对、空文本、超过 20 个附件、未知 command。

- [ ] **Step 2: 写事件映射参数化测试**

```python
@pytest.mark.parametrize(("event", "part_kind"), [
    (_output_delta("A"), "text"),
    (_thinking_delta("B"), "reasoning"),
    (_tool_started(), "tool-call"),
    (_file_changed(), "file-change"),
])
def test_projector_maps_runtime_event(event: RuntimeEvent, part_kind: str) -> None:
    state = apply_runtime_event(_empty_state(), event)
    assert part_kind in _serialized_parts(state)
```

- [ ] **Step 3: 实现 schema 和 state projector，不改 RuntimeEvent 或 payload 模型**

投影器只读取 `event.event_type` 和已定义 payload 字段：

```python
EVENT_DATA_NAMES: dict[EventType, str] = {
    EventType.DELEGATION_STARTED: "delegation",
    EventType.FILE_CHANGE_UPDATED: "file-change",
    EventType.CONTEXT_USAGE: "context-usage",
    EventType.HUMAN_INPUT_REQUESTED: "approval",
}
```

`model_output_delta`、`model_thinking_delta`、`tool_call_started`、`tool_call_finished`、`run_finished` 与失败/取消事件更新固定 assistant message/run DTO；不能可靠表达的事件统一写入 named custom entry，保留原始 `event_id`、`task_id`、`turn_id`、`sequence` 与 `payload`。`final_response` 只负责收口/校准，不得再次追加已由 output delta 写入的文本。

- [ ] **Step 4: 生成共享 TS 契约并运行定向检查**

Run:

```powershell
uv run python scripts/generate_api_ts.py
uv run pytest tests/test_assistant_transport_state_projector.py -q
uv run ruff check app/api/assistant_transport app/api/schemas/request/AssistantTransportRunRequest.py app/api/schemas/response/AssistantTransportStateResponse.py
```

Expected: 所有 27 类运行时事件有确定 state 投影或明确 custom-entry 回退；生成物无手工编辑。

- [ ] **Step 5: 复跑旧 SSE 端点相关测试**

Run:

```powershell
uv run pytest tests/test_runtime_event_bus_backpressure.py tests/test_turn_usage_stats.py -q
```

Expected: 旧 `RuntimeEvent.to_dict()` 和订阅语义不变。

---

### Task 3: 以服务层实现 command 到 Task/Turn 的原子映射

**Files:**

- Create: `apps/backend/app/service/task/assistant_transport_service.py`
- Modify: `apps/backend/app/service/depends.py`
- Modify: `apps/backend/app/api/dependencies.py`
- Create: `apps/backend/tests/test_assistant_transport_service.py`

**Interfaces:**

- `AssistantTransportService.start_run(request: AssistantTransportRunRequest) -> AssistantTransportRun`：新 thread 时创建 Task + 首 Turn；已有 thread 时只创建 Turn。
- `AssistantTransportRun`：`task_id: int`、`turn_id: int`、`thread_id: str`、`run_id: str`。
- `AssistantTransportService.get_state(task_id: int) -> AssistantTransportStateResponse`：从已有 Task/Turn/运行时事件重建历史快照。

- [ ] **Step 1: 写失败测试，固定新 thread 只有一次服务调用链**

```python
def test_start_run_creates_task_and_first_turn_from_one_request() -> None:
    run = service.start_run(_new_thread_request("make a plan"))
    assert run.task_id > 0
    assert run.turn_id > 0
    assert turn_service.get_turn(run.turn_id).task_id == run.task_id
```

此测试禁止 API/客户端先取得乐观 task ID 再创建 turn。

- [ ] **Step 2: 写失败测试，固定已有 task 和附件语义**

```python
def test_start_run_uses_existing_task_and_forwards_non_image_attachments() -> None:
    run = service.start_run(_existing_task_request(task_id=3, attachments=[_file_ref()]))
    assert run.task_id == 3
    assert turn_service.get_turn(run.turn_id).input_text.startswith("[附件]")
```

- [ ] **Step 3: 实现 service，只复用既有领域服务**

内部调用现有 TaskService/TurnService；禁止读取 CRUD 或 ORM。命令解析只接受一个 user `add-message`，把 text 与 request 中结构化附件合并后调用既有 `TurnService.create_turn()`；模型二元组和 `reasoning_effort` 原样透传。

- [ ] **Step 4: 编写历史快照转换测试**

```python
def test_get_state_keeps_turn_order_and_stable_ids() -> None:
    state = service.get_state(task_id=3)
    assert [m["id"] for m in state.messages] == [
        "task:3:turn:10:user", "task:3:turn:10:assistant"
    ]
```

历史转译应优先使用持久化 turn/event，不引入新的 messages 表；对于 data part 必须保存 `event_id` 以供前端去重。

- [ ] **Step 5: 运行服务层回归测试**

Run:

```powershell
uv run pytest tests/test_assistant_transport_service.py tests/test_attachment_handling.py -q
```

Expected: 新旧创建 turn 路径都保持模型、附件和 task 归属正确。

---

### Task 4: 新增并行 Transport API、取消和恢复端点

**Files:**

- Create: `apps/backend/app/api/assistant_transport_api.py`
- Create: `apps/backend/tests/test_assistant_transport_api.py`
- Modify: `apps/backend/app/app.py`

**Interfaces:**

- `POST /assistant-transport/runs`：接收 `AssistantTransportRunRequest`，返回 Assistant Transport state-operation stream。
- `GET /assistant-transport/tasks/{task_id}/state`：返回 `AssistantTransportStateResponse`。
- `POST /assistant-transport/runs/{turn_id}/cancel`：复用既有取消语义，幂等返回 run 状态。
- `POST /assistant-transport/runs/resume-state` 与 `POST /assistant-transport/runs/resume`：只在 Task 1 的审批/断线 Spike 证明可用后实现；无可恢复 run 返回 204。

- [ ] **Step 1: 写 API 失败测试，验证启动和旧端点隔离**

```python
async def test_transport_run_streams_assistant_transport_protocol(client: AsyncClient) -> None:
    response = await client.post("/assistant-transport/runs", json=_new_thread_body())
    body = (await response.aread()).decode()
    assert "aui-state:" in body
    assert '"is_running"' in body
```

另写一例继续调用 `GET /turns/{turn_id}/stream`，断言其 event/data 格式仍是 `RuntimeEvent.to_dict()`。

- [ ] **Step 2: 写取消与客户端中断测试**

```python
async def test_transport_cancel_marks_only_target_turn(client: AsyncClient) -> None:
    await client.post("/assistant-transport/runs/12/cancel")
    assert turn_service.get_turn(12).status == "cancelled"
    assert turn_service.get_turn(13).status == "running"
```

覆盖 fetch abort 后显式 cancel 不把正常的用户取消错误标为 `failed`。

- [ ] **Step 3: 实现薄 API 层并注册路由**

`assistant_transport_api.py` 只负责获取 `AssistantTransportService`、`AgentRuntime`、`TurnStreamService`，然后将

```python
stream_service.stream_turn_events(runtime.run_turn, turn)
```

逐条投影到 `AssistantTransportThreadState`，再由官方 `assistant_stream.create_run` / `DataStreamResponse` 发出 state-operation stream。在 `app.py` 中增加：

```python
importlib.import_module("app.api.assistant_transport_api")
```

必须使用 `importlib.import_module`，不能使用普通 `import app.api...`。

- [ ] **Step 4: 实现状态和恢复端点，并限定其语义**

状态端点从 Task/Turn/事件历史重建，不引入第二份持久化事实；它必须携带可回放的 `cursor`，并在 live 订阅前以 event `sequence` 补齐 cursor 后的持久化事件。恢复端点只恢复仍处于 `running` 且存在 checkpoint 的 turn。审批尚未完成前，state 写入 approval entry 并保持 run 可恢复，而不是伪造成功 finish。

- [ ] **Step 5: 运行 API、运行时和取消回归测试**

Run:

```powershell
uv run pytest tests/test_assistant_transport_api.py tests/test_runtime_event_bus_backpressure.py tests/test_delegation_executor_fail_finalize.py -q
```

Expected: transport 流工作，旧 SSE 与 delegation 不倒退。

---

### Task 5: 建立前端 transport client 和集中式 runtime adapter

**Files:**

- Create: `apps/desktop/src/services/assistantTransport.ts`
- Create: `apps/desktop/src/services/assistantTransportState.ts`
- Create: `apps/desktop/src/components/chat/AssistantConversationRuntime.tsx`
- Create: `apps/desktop/src/tests/assistantTransportState.test.ts`
- Create: `apps/desktop/src/tests/AssistantConversationRuntime.test.tsx`
- Modify: `apps/desktop/src/services/api.ts`

**Interfaces:**

- `fetchAssistantTransportState(taskId: number): Promise<AssistantTransportConversationState>`。
- `toAssistantTransportState(state, metadata): AssistantTransportState`：纯函数，`isRunning` 仅由该 task 的 run 决定。
- `AssistantConversationRuntime({ taskId, children })`：唯一调用 `useAssistantTransportRuntime()` 的组件。

- [ ] **Step 1: 写纯转换失败测试**

```ts
it("does not mix messages from background tasks", () => {
  const state = toAssistantTransportState(snapshotForTask(9), metadata);
  expect(state.messages.every((message) => message.id.startsWith("task:9:"))).toBe(true);
});
```

同时覆盖 stable message ID、已完成历史、running turn、delegation/data part 去重。

- [ ] **Step 2: 写 runtime 请求失败测试**

```tsx
expect(fetchMock).toHaveBeenCalledWith(
  expect.stringContaining("/assistant-transport/runs"),
  expect.objectContaining({ method: "POST" }),
);
```

断言 `prepareSendCommandsRequest` 只发送一个 user command，并从 model store 取成对的 `provider_id/model_name`、当前 `reasoning_effort` 和附件快照。

- [ ] **Step 3: 实现 API client 和 state converter**

`assistantTransport.ts` 复用 `httpClient` 的错误规范；新建 task 时 body 含 `workspace_id` 且不含 `task_id`，已有 task 时反之。`assistantTransportState.ts` 不读取 Zustand、DOM 或网络，以便单测。

- [ ] **Step 4: 实现 runtime provider 和取消桥接**

```tsx
const runtime = useAssistantTransportRuntime({
  api: API_PATHS.ASSISTANT_TRANSPORT_RUNS,
  protocol: "assistant-transport",
  initialState,
  converter: toAssistantTransportState,
  prepareSendCommandsRequest: buildTransportRequest,
  onCancel: ({ updateState }) => void cancelAssistantTransportRun(activeTurnId),
});
```

不要启用 client tool invocations；`onError` 必须调用 `updateState` 恢复本地 draft/附件快照，而不是仅恢复 InputBar 局部 state。

- [ ] **Step 5: 运行定向测试和 lint**

Run:

```powershell
npm.cmd test -- --run src/tests/assistantTransportState.test.ts src/tests/AssistantConversationRuntime.test.tsx
npx.cmd eslint src/services/assistantTransport.ts src/services/assistantTransportState.ts src/components/chat/AssistantConversationRuntime.tsx
```

Expected: 背景 task 事件不进入前台 thread；失败、取消和重复点击不丢草稿。

---

### Task 6: 迁移 Composer，并显式桥接项目附件能力

**Files:**

- Create: `apps/desktop/src/components/layout/AssistantComposer.tsx`
- Modify: `apps/desktop/src/components/layout/InputBar.tsx`
- Modify: `apps/desktop/src/components/layout/useAttachmentInput.ts`
- Modify: `apps/desktop/src/components/layout/AttachmentChip.tsx`
- Create: `apps/desktop/src/tests/AssistantComposer.test.tsx`
- Modify: `apps/desktop/src/tests/InputBar.attachment.test.tsx`

**Interfaces:**

- `AssistantComposer` 使用 `ComposerPrimitive.Root`、`ComposerPrimitive.Input`、`ComposerPrimitive.Send` 和 `ComposerPrimitive.Cancel`。
- `useAttachmentInput` 继续输出 `AttachmentRef[]`；提交时由 transport request builder 外带，不把 file/directory/url 强转为图片 URL。

- [ ] **Step 1: 写失败测试，固定 UI 基础结构和 IME 语义**

```tsx
expect(screen.getByRole("textbox")).toBeInTheDocument();
expect(screen.getByRole("button", { name: "发送" })).toBeEnabled();
fireEvent.keyDown(input, { key: "Enter", isComposing: true });
expect(send).not.toHaveBeenCalled();
```

- [ ] **Step 2: 写失败测试，固定附件的发送/回滚语义**

```tsx
await user.click(screen.getByRole("button", { name: "发送" }));
expect(buildTransportRequest).toHaveBeenCalledWith(
  expect.objectContaining({ attachments: [{ kind: "url", ref: "https://example.com" }] }),
);
```

同时断言 transport 失败后输入文本和 chips 都恢复，成功后两者才清空。

- [ ] **Step 3: 将 InputBar 收敛为外壳，迁入 AssistantComposer**

保留 `ModelSelector`、Provider 设置入口、`useModelSendGuard`、上下文圆环和项目现有附件选择器；删除 InputBar 内部对 `createTask/createTurn` 的直接调用。创建/发送由 Assistant UI command queue 驱动，InputBar 不再自行维护乐观 task/turn ID。

- [ ] **Step 4: 实现停止与无活跃 run 状态**

运行中展示 `ComposerPrimitive.Cancel`，由 Task 5 的 cancel bridge 精确取消当前 transport `turn_id`。无 task 时不显示 0/0 上下文圆环；后台其它 task 运行不得禁用当前 Composer。

- [ ] **Step 5: 运行附件、发送和可访问性回归**

Run:

```powershell
npm.cmd test -- --run src/tests/AssistantComposer.test.tsx src/tests/InputBar.attachment.test.tsx src/tests/useAttachmentInput.test.ts
npx.cmd eslint src/components/layout/AssistantComposer.tsx src/components/layout/InputBar.tsx src/components/layout/useAttachmentInput.ts
```

Expected: 现有 4 类附件、IME、失败回滚和取消均保持可用。

---

### Task 7: 迁移消息渲染，并把差异化事件收敛为 data renderers

**Files:**

- Create: `apps/desktop/src/components/chat/AssistantDataParts.tsx`
- Create: `apps/desktop/src/components/chat/AssistantThread.tsx`
- Modify: `apps/desktop/src/components/layout/ChatPanel.tsx`
- Modify: `apps/desktop/src/components/chat/AgentMessage.tsx`
- Modify: `apps/desktop/src/components/chat/ThinkingBlock.tsx`
- Modify: `apps/desktop/src/components/chat/ToolCallCard.tsx`
- Create: `apps/desktop/src/tests/AssistantThread.test.tsx`
- Create: `apps/desktop/src/tests/AssistantDataParts.test.tsx`

**Interfaces:**

- `AssistantThread` 使用 `ThreadPrimitive.Messages`，标准 text/reasoning/tool part 映射到现有视觉组件。
- `AssistantDataParts` 对稳定 name 注册 `delegation`、`file-change`、`context-usage`、`tool-output`、`approval`。

- [ ] **Step 1: 写 data renderer 失败测试**

```tsx
render(<AssistantDataParts part={{ type: "data", name: "delegation", data: delegation }} />);
expect(screen.getByText(/子 Agent/)).toBeInTheDocument();
```

分别覆盖：delegation 完成/失败、文件变更、终端增量、上下文用量和审批等待；未知 name 必须降级为可复制 JSON，而不是抛异常。

- [ ] **Step 2: 写标准 part 视觉回归测试**

```tsx
render(<AssistantThread state={fixtureWithReasoningAndTool()} />);
expect(screen.getByText("思考过程")).toBeInTheDocument();
expect(screen.getByText("执行工具")).toBeInTheDocument();
```

- [ ] **Step 3: 实现 renderer 复用，不复制既有业务 UI**

 thinking 重用 `ThinkingBlock`，tool part 重用 `ToolCallCard`/`TerminalCallCard`，delegation 重用 `DelegationTimelineEntry`，文件变更继续驱动 `ChangesDrawer` 的现有查询逻辑。`AssistantConversationRuntime` 要把 state 中最新的 context usage 写入既有 `contextUsageStore(taskId)`，把 `file-change.stable` 作为 `useChanges` 刷新触发信号；Changes API 仍是权威来源。首期保留 `useDelegationStreams` 对 child turn 的独立订阅，不能把 child 运行细节扁平进父 assistant message。Assistant UI 仅替代父对话的消息编排与流式 part 生命周期。

- [ ] **Step 4: 将 ChatPanel 接到 runtime provider，但保留 TaskHeaderBar 和右栏**

ChatPanel 根据 `activeTaskId` 拉取 transport snapshot 并挂载 `AssistantConversationRuntime`；`TaskHeaderBar`、`ChangesDrawer`、`SubagentPanel` 和 workspace 选择不迁入 Assistant UI。切换 task 时以 `task:<id>` 为 key 销毁旧 runtime，防止跨 task 串流。

- [ ] **Step 5: 运行渲染、切换和性能回归**

Run:

```powershell
npm.cmd test -- --run src/tests/AssistantThread.test.tsx src/tests/AssistantDataParts.test.tsx src/tests/useSSE.contextUsageIsolation.test.tsx
```

Expected: 后台 task 事件不串入、思考/工具/委派/变更仍可见，长消息只更新活跃 part。

---

### Task 8: 灰度切换、回归闭环与旧链路清理决策

**Files:**

- Modify: `apps/desktop/src/App.tsx`
- Modify: `apps/desktop/src/hooks/useTask.ts`
- Modify: `apps/desktop/src/hooks/useSSE.ts`
- Modify: `apps/desktop/src/services/timeline/projector.ts`（只在切换完成后删除）
- Modify: `apps/desktop/src/components/layout/TurnTimeline.tsx`（只在切换完成后删除）
- Modify: `docs/plan/assistant-ui-migration-plan.md`
- Create: `apps/desktop/src/tests/assistantUiMigration.e2e.test.tsx`

**Interfaces:**

- 临时 feature flag：`VITE_ASSISTANT_UI_TRANSPORT_ENABLED`；默认关闭直到验收完成。
- 任意失败可回退至现有 InputBar + `useTask` + `useSSE` + TurnTimeline，不迁移数据库。

- [ ] **Step 1: 写端到端失败测试，覆盖关键业务闭环**

```tsx
it("creates a new task, streams a tool call, and keeps draft after transport error", async () => {
  render(<App />);
  await user.type(screen.getByRole("textbox"), "inspect this repo");
  await user.click(screen.getByRole("button", { name: "发送" }));
  expect(await screen.findByText("执行工具")).toBeInTheDocument();
  expect(screen.getByRole("textbox")).toHaveValue("");
});
```

另设失败流用例，断言 draft 和附件恢复；再设两个 task 并发流用例，断言消息和 context usage 均按 task 隔离。

- [ ] **Step 2: 接入 feature flag，双链路不共享写入所有权**

flag 开启时 App 使用 `AssistantConversationRuntime`/`AssistantComposer`，并停止为同一 turn 建立旧 `useSSE` 连接；flag 关闭时完全沿用旧链路。禁止同一 turn 同时被 transport 和 `useSSE` 消费。

- [ ] **Step 3: 做人工验收**

在桌面端依次验证：新建任务、已有任务追加、模型切换、文本/图片/文件/目录/URL 附件、工具调用、终端输出、委派、变更集、取消、网络中断恢复和多 task 并发。每项记录 transport 或 legacy 路径、task ID、turn ID 和最终状态。

- [ ] **Step 4: 执行完整相关测试与静态检查**

Run:

```powershell
uv run pytest tests/test_assistant_transport_contract_spike.py tests/test_assistant_transport_state_projector.py tests/test_assistant_transport_service.py tests/test_assistant_transport_api.py tests/test_attachment_handling.py tests/test_delegation_executor_fail_finalize.py -q
npm.cmd test -- --run src/tests/assistantTransportState.test.ts src/tests/AssistantConversationRuntime.test.tsx src/tests/AssistantComposer.test.tsx src/tests/AssistantThread.test.tsx src/tests/AssistantDataParts.test.tsx src/tests/assistantUiMigration.e2e.test.tsx
npx.cmd eslint src/services/assistantTransport.ts src/services/assistantTransportState.ts src/components/chat/AssistantConversationRuntime.tsx src/components/chat/AssistantDataParts.tsx src/components/chat/AssistantThread.tsx src/components/layout/AssistantComposer.tsx
```

Expected: 全部通过；`npm.cmd run build` 的输出与迁移前基线比较，不允许新增本次文件相关报错。

- [ ] **Step 5: 只在连续灰度通过后删除旧投影层**

完成连续人工验收和自动化回归后，删除 `services/timeline/projector.ts`、`services/timeline/groupTools.ts`、`TurnTimeline.tsx` 与被迁出的 InputBar 发送状态机；同步删除仅服务旧链路的测试。若任一验收失败，保留 feature flag 和旧链路，修复 transport 后重新验收，不做半删半留。

---

### Task 9: 在需要“重启后完整富消息与异步审批”时补齐领域持久化

**Prerequisite:** Task 8 的适配层已稳定运行，并且产品明确要求历史对话无损恢复图片、文件、reasoning、tool/data parts，以及桌面重启后继续处理人工审批。若只交付 Assistant UI 展示层，本任务不启动。

**Files:**

- Create: `apps/backend/app/models/turn_attachment_record.py`
- Create: `apps/backend/app/models/turn_message_part_record.py`
- Create: `apps/backend/app/models/human_approval_request_record.py`
- Create: `apps/backend/app/storage/model/turn_attachment_model.py`
- Create: `apps/backend/app/storage/model/turn_message_part_model.py`
- Create: `apps/backend/app/storage/model/human_approval_request_model.py`
- Create: `apps/backend/app/storage/crud/turn_attachment_crud.py`
- Create: `apps/backend/app/storage/crud/turn_message_part_crud.py`
- Create: `apps/backend/app/storage/crud/human_approval_request_crud.py`
- Create: `apps/backend/app/service/task/human_approval_service.py`
- Create: `apps/backend/app/api/approvals_api.py`
- Modify: `apps/backend/app/storage/init_schema.py`
- Modify: `apps/backend/app/storage/cascade_deletion.py`
- Modify: `apps/backend/app/service/task/turn_service.py`
- Modify: `apps/backend/app/core/workflows/nodes/helper/approval.py`
- Modify: `apps/backend/app/core/runtime/runner.py`
- Create: `apps/backend/tests/test_turn_attachment_persistence.py`
- Create: `apps/backend/tests/test_human_approval_service.py`
- Create: `apps/backend/tests/test_human_approval_resume.py`

**Interfaces:**

- `TurnAttachmentRecord(turn_id, kind, ref, mime_type, filename, size, provenance)`：原始 `AttachmentRef` 的可回放事实，不再只压扁进 `input_text`。
- `TurnMessagePartRecord(message_id, turn_id, sequence, part_type, payload_json)`：用于无损恢复 text/reasoning/tool/data/image/file 的已定稿 part；不可取代 runtime event 审计流。
- `HumanApprovalRequestRecord(request_id, task_id, turn_id, checkpoint_thread_id, step_id, tool_call_ids, status, decision, reason)`：审批与 checkpoint 的领域事实。
- `POST /turns/{turn_id}/approvals/{request_id}/resolve`：只允许 pending request 一次性变为 approved/rejected，并触发相同 checkpoint 的 resume。

- [ ] **Step 1: 写附件历史回放失败测试**

```python
def test_turn_attachment_round_trip_keeps_file_and_url_structure() -> None:
    turn_service.create_turn(3, "inspect", attachments=[_file_ref(), _url_ref()])
    assert attachment_crud.list_by_turn(1) == [_file_ref(), _url_ref()]
```

测试必须证明历史回放无需从 `input_text` 反解析附件；旧 turn 没有结构化附件时明确返回“仅文本历史”，不得伪造。

- [ ] **Step 2: 写审批幂等与 checkpoint 绑定失败测试**

```python
def test_resolve_approval_rejects_second_decision_and_wrong_turn() -> None:
    request = approval_service.create_pending(_pending_tool_calls())
    approval_service.resolve(request.request_id, turn_id=request.turn_id, approved=True, reason=None)
    with pytest.raises(ApprovalAlreadyResolvedError):
        approval_service.resolve(request.request_id, turn_id=request.turn_id, approved=False, reason="no")
```

同时覆盖进程重启后查询 pending request、拒绝不执行工具、批准只从原 checkpoint 恢复一次。

- [ ] **Step 3: 新增表、CRUD、级联删除和 API schema**

在 `init_schema.py` 以明确 migration 版本创建三张表和索引；在 `cascade_deletion.py` 将它们纳入 task/turn 删除事务。`turn_service.create_turn()` 在创建时持久化原始附件；message-part 只由 transport projector 在 part 定稿后写入，避免每个 token 产生一行数据库写入。

- [ ] **Step 4: 将 LangGraph interrupt 接到领域审批服务**

`approval.py` 不再只依赖进程内同步 callback：先持久化 pending request、发出真实 `human_input_requested` 事件并让 turn 进入可恢复状态；resolve API 写入决策后调用既有 `Command(resume=...)` 路径。`RuntimeConfig` 仍是运行时配置，审批 request ID 和 decision 不写入它。

- [ ] **Step 5: 运行持久化、审批与回归测试**

Run:

```powershell
uv run pytest tests/test_turn_attachment_persistence.py tests/test_human_approval_service.py tests/test_human_approval_resume.py tests/test_attachment_handling.py tests/test_delegation_executor_fail_finalize.py -q
uv run ruff check app/models app/storage app/service/task/human_approval_service.py app/api/approvals_api.py
```

Expected: 领域审批与附件可在进程重启后恢复；Assistant UI 只消费投影，绝不成为权限、checkpoint 或附件的事实源。

---

## 迁移完成判定

- 新 thread 与已有 task 的发送各只触发一次后端 run 创建，且真实 Task/Turn ID 在首帧前已确定。
- text、thinking、tool、delegation、file change、context usage、terminal output、审批事件均有确定渲染或可读降级。
- 文本、图片、文件、目录、URL 附件不丢失，失败和取消不丢草稿或附件。
- 多 task 并发时任一消息、运行状态和上下文用量均不跨 task 串写。
- 旧 SSE API、LangGraph checkpoint、服务端工具执行、delegation 和变更集能力保持可用。
- 在上述条件未连续通过前，`VITE_ASSISTANT_UI_TRANSPORT_ENABLED` 默认关闭且旧链路可回退。
- 若 Task 9 已启动，附件、message parts 与审批请求的新增表必须通过删除级联、重启恢复和旧数据“仅文本历史”三类测试。
