# Assistant Transport 增量快照改造方案

> 状态：已实施，架构与测试验收通过。
>
> 本文只覆盖 Assistant Transport 增量 state、Task 快照和 tool-call 生命周期传输。
> LLM context 构建、context compaction 和 context 持久化格式暂不纳入本次改造。

## 1. 已确认决策

1. 使用官方 Assistant Transport 的 `set` / `append-text` 增量操作。
2. 一个 `Task` 维护一个完整 JSON `ConversationStateSnapshot`。
3. 初次连接发送完整 snapshot，后续只发送局部变更。
4. 正常流式路径不再每个 chunk 查询数据库、重建完整 snapshot、再做全量 diff。
5. Runtime mutation 直接更新 snapshot，并同步生成 Transport mutation。
6. 文本追加使用 `append-text`。
7. 消息状态、tool 状态、tool result、error、cancelled 使用最小路径的 `set`。
8. 第一阶段支持完整 tool-call 生命周期；Agent 层暂不支持工具参数 JSON 的逐 token 流式化。
9. snapshot 持久化到独立的一对一 `conversation_task_snapshots` 表。
10. `controller.state` 是某个 HTTP 连接的 Transport 副本，不是新的业务事实源。
11. 不恢复 `revision`、`conversation_heads` 或 `conversation_changes`。
12. 后续可用进程内 notifier 替代当前 `50ms` 轮询，但 notifier 不是事实源。

目标关系：

```text
Task 1 ─── 1 ConversationTaskSnapshot
                │
                ├── SQLite 持久化 JSON
                └── 进程内当前快照
                         │
                    set / append-text
                         ↓
                    controller.state
                         ↓
                      assistant-ui
```

## 2. 官方协议依据

官方 Assistant Transport 将后端 state 定义为任意 JSON 对象，前端通过 converter 将 state
映射为 assistant-ui 消息。官方示例：

```python
controller.state["message"] = "Hello"      # set
controller.state["message"] += " World"     # append-text
```

官方 `assistant-stream` 用两类操作复制复杂 JSON：

- `set`：设置值或结构；
- `append-text`：向指定路径追加文本。

参考：<https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport>

因此当前：

```python
controller.state = snapshot
```

只能作为完整 state 初始化或安全 fallback，不能作为正常的增量流式路径。目标实现必须
在 Assistant Transport API 边界内修改 `controller.state` 的嵌套路径。

## 3. 当前代码事实

### 3.1 当前全量流

入口是：

```text
apps/backend/app/assistant_transport/assistant_api.py
```

`_subscribe_run_state()` 的目标实现已执行：

```python
async for change in subscription.stream(...):
    for mutation in change.mutations:
        apply_mutation(controller, mutation)
```

`ConversationRunSubscriptionService` 位于：

```text
apps/backend/app/service/task/conversation_run_subscription_service.py
```

它首帧发送一个 root `set` 完整 snapshot，后续消费 snapshot owner 提交的局部 mutation；
当前保留 50ms 轮询仅作为无 notifier 时的低频兜底，不再每个 chunk 从旧 conversation 表
重建 snapshot 或做全量 diff。

### 3.2 当前 Runtime mutation

模型节点：

```text
apps/backend/app/core/workflows/nodes/model_node.py
```

通过 `model.astream(messages)` 消费 chunk，并调用：

```python
operations.append_assistant_text(text)
operations.append_assistant_reasoning(reasoning)
```

`RuntimeOperations` 位于：

```text
apps/backend/app/core/runtime/runtime_operations.py
```

它通过 `ConversationMutationWriter` 写入 assistant text/reasoning、tool-call 状态、tool
结果以及 run 终态。Runtime 已知道每个 mutation 的意图，不需要再从两个完整 JSON 推断变化。

### 3.3 当前 tool 事实

`ConversationStateService._tool_call_part()` 投影的中性 tool part 包含：

```json
{
  "type": "tool-call",
  "toolCallId": "call-1",
  "toolName": "read_file",
  "status": "pending",
  "args": {},
  "result": {},
  "error": "...",
  "isError": true
}
```

现有生命周期由 `RuntimeOperations` / `ConversationMutationWriter` 驱动，并同步发出最小
路径的 `set` mutation：

```text
create_tool_call
→ pending
→ running / requires-action
→ completed / failed / cancelled
```

前端 [converter.ts](H:/coding-agent/apps/desktop/lib/assistant/converter.ts) 负责把这些中性
字段映射到 assistant-ui 可消费的消息状态、result/error envelope 和 `isError`；由于当前
安装版本的 `ToolCallMessagePart` 没有独立可写的 `status` 字段，tool 状态通过消息状态与
tool result/error/cancelled envelope 表达，不向 assistant-ui 类型伪造不存在的字段。

## 4. 目标状态模型

### 4.1 一 Task 一快照

新增独立的一对一表：

```text
conversation_task_snapshots
```

建议 ORM 字段：

```python
task_id         INTEGER PRIMARY KEY REFERENCES tasks(id)
state_json      TEXT NOT NULL
schema_version  INTEGER NOT NULL DEFAULT 1
updated_at      TEXT NOT NULL
```

字段规则：

- 逻辑上 `task_id` 是一对一主键；由于项目所有 ORM 统一继承 `StorageBase`，当前物理实现
  保留公共 surrogate `id`，并对 `task_id` 建立 `UNIQUE` 约束，效果等价于每个 Task 只有
  一份持久化 snapshot；
- `state_json` 保存完整 JSON 文档；
- `schema_version` 用于结构演进，不是并发 revision；
- 不新增 `command_id`、`run_id`、`revision`、lease 或 fencing 字段；
- 不把大 JSON 直接放进 `tasks`，避免任务元数据查询携带完整对话内容。

SQLite 使用 `TEXT` 存 JSON，结构由应用层 `ConversationStateSnapshot` 契约校验。

### 4.2 快照的权威范围

`ConversationTaskSnapshot` 是 Assistant Transport、对话页面、断线重连和运行状态展示的
canonical state。它不是 LLM context，也不承担 context 构建规则；context 在本阶段继续
使用当前独立的 runtime/context 路径，后续另行改造。

现有 `conversation_messages`、`conversation_message_parts`、`conversation_tool_calls`
不能继续作为 Transport 的第二事实源。实施目标是一次性完成读取/写入边界切换：

- Transport 的历史、实时更新和重连只读 `conversation_task_snapshots`；
- Runtime mutation 的 UI 可见部分只由 snapshot owner 更新；
- 旧 conversation 表在 context 尚未迁移期间只能作为明确隔离的 context 兼容来源，不能被
  Assistant Transport 读取，也不能被称为 Transport canonical state；
- context 迁移完成后，旧 conversation 表必须删除或降级为明确的领域索引，不能长期双读、
  双写；
- 如果 snapshot 与旧表不一致，snapshot owner/迁移校验负责报告并修复，Transport 不自行
  从旧表回填。

这是一段受控的事实模型迁移，不是长期保留两份可写 canonical state。

### 4.3 快照所有权

新增明确的 `ConversationTaskSnapshotStore`（名称可按实现调整）作为唯一快照 owner，负责：

- 创建 Task 初始 snapshot；
- 加载和保存 Task snapshot；
- 按 Runtime mutation 更新 snapshot；
- 向 Transport stream 发布已提交的 mutation；
- 保存 run 终态。

不允许 `assistant_api.py`、`ConversationStateService`、`RuntimeOperations` 各自维护一份
业务 JSON。它们只能通过 snapshot port 读取或提交 mutation。`RuntimeContextManager` 是
LLM context 的运行时容器，不得从 `controller.state` 或 Transport snapshot 反向充当 context
事实源；本阶段不改变它的构建规则。

需要区分物理副本，但不把它们当成多份事实：

```text
SQLite snapshot       → 重启/重连 hydrate
进程内 Task snapshot  → 当前 run 的实时工作对象
controller.state      → 单个 HTTP 连接的协议副本
```

## 5. Mutation 设计

### 5.1 不做完整 snapshot diff

不再使用：

```text
old_snapshot vs new_snapshot
```

因为 Runtime mutation 已经提供了明确意图。每个 mutation 应同时：

1. 在唯一 owner 内更新当前 `ConversationStateSnapshot`；
2. 生成中性的 Transport mutation；
3. 在同一个 SQLite 写事务内更新持久化 snapshot；
4. 事务提交成功后，应用到连接的 `controller.state`。

建议在 `apps/backend/app/assistant_transport/stream/` 内定义 Transport 边界类型，
例如：

```python
class ConversationStateMutation(TypedDict):
    kind: Literal["set", "append-text"]
    path: tuple[str | int, ...]
    value: object
```

它不是 RuntimeEvent、数据库 change log 或新的持久化事实。

### 5.2 文本

```text
append_assistant_text("abc")
  → snapshot 对应 text 追加 "abc"
  → append-text(path, "abc")
  → snapshot 持久化
  → commit 后发送
```

路径使用稳定消息/part 定位：

```text
["messages", assistant_index, "parts", text_part_index, "text"]
```

`append-text.value` 只能是本次新增文本，不能是完整文本。

### 5.3 消息和 part

消息或 part 新增时，只设置对应位置：

```text
set ["messages", message_index] = new_message
set ["messages", message_index, "parts", part_index] = new_part
```

状态变化只设置最小字段：

```text
set ["messages", message_index, "status"] = "completed"
set ["messages", message_index, "endReason"] = "..."
set ["messages", message_index, "parts", part_index, "status"] = "complete"
```

正常路径禁止替换整个 `messages` 数组。只有删除、重排、ID 不一致或连接基线无法确认时，
才允许按以下顺序 fallback：

```text
单字段 → 单个 part → 单条 message → messages → 根 state
```

### 5.4 Tool-call

第一阶段必须支持以下生命周期：

```text
pending → running → completed
pending/running → requires-action
pending/running → failed
pending/running → cancelled
```

创建 tool part：

```text
set ["messages", assistant_index, "parts", tool_index] = {
  "type": "tool-call",
  "toolCallId": "call-1",
  "toolName": "read_file",
  "args": {"path": "README.md"},
  "status": "pending"
}
```

状态变化：

```text
set [..., "status"] = "running"
set [..., "status"] = "requires-action"
set [..., "status"] = "completed"
set [..., "status"] = "failed"
set [..., "status"] = "cancelled"
```

结果和错误：

```text
set [..., "result"] = result
set [..., "error"] = error
set [..., "isError"] = true
```

审批摘要如已有领域事实，应写入明确的 `approval` 字段，不允许前端通过字段存在与否猜测
审批状态。

当前 Agent 层创建 tool call 时已经拥有结构化 arguments，因此本阶段不支持 `argsText` 的
逐字符流。未来 Agent 层提供参数流时，再增加对应的 `append-text` 路径。

官方 Assistant Transport 只规定 JSON state 的 `set` / `append-text`，不规定本项目这些
tool 领域状态的含义。领域状态必须在后端 snapshot 中保持明确；前端 converter 再依据已
安装包的实际 `ToolCallMessagePart` 不包含可写的 `status` 字段；assistant-ui 通过助手消息
status、tool result/error/cancelled envelope 和 `isError` 推导 tool 展示状态。因此后端
仍保持明确的 tool 领域状态，converter 不伪造一个类型系统不存在的 part 字段，而是做
如下确定性映射：

| 后端 snapshot 状态 | 前端映射要求 |
|---|---|
| `pending` / `running` | assistant message status 为 `running`；tool 没有 result 时由 assistant-ui 显示 running |
| `requires-action` | assistant message status 为 `{ type: "requires-action", reason: "tool-calls" }` |
| `completed` | 设置 `result`（即使值为 `null`），无 `isError`，由 assistant-ui 识别为完成 |
| `failed` | 设置明确的 error envelope、`isError: true`，并保留失败状态；ToolFallback 显示失败 |
| `cancelled` | 设置 cancelled envelope；ToolFallback 显示取消 |

实现不得依赖 result 是否存在来推断后端领域状态；completed/failed/cancelled 都由明确
的后端状态驱动。`pending` 虽然保留在后端 snapshot，但 assistant-ui 没有 pending 类型，
因此通过父消息 running 表达。最终字段必须以当前安装包类型和现有 ToolFallback 组件实际
支持的 props 为准；不能把这张表误称为 Assistant Transport 官方 tool 状态规范。验收必须
覆盖空 result、失败、取消和 requires-action。

## 6. 事务和生命周期

### 6.1 原子提交

领域事实和 snapshot 必须在同一个 SQLite 写事务中提交：

```text
ConversationMutationWriter
  ├── 更新 run/message/tool 事实
  ├── 更新 conversation_task_snapshots.state_json
  └── commit
          ↓
      notify Transport subscribers
```

必须先 commit 再通知。通知失败不能回滚已提交事实；发送失败必须能通过 snapshot 重连恢复。

### 6.2 运行时并发

当前单后端进程、单 `ConversationRunExecutor`、每个 run 一个 `asyncio.Task`，且已有
per-task lock / `claim_pending_run()`，可以保证同一 Task 的 active run mutation 有确定顺序。

多个 HTTP 订阅可以读取同一个 Task snapshot，但每个连接拥有自己的 `controller.state` 副本；
只有 snapshot owner 能修改业务快照。

### 6.3 进程重启

进程重启丢失进程内快照，但保留 SQLite snapshot：

```text
读取 conversation_task_snapshots
→ hydrate ConversationStateSnapshot
→ 初始化新的 controller.state
```

遗留 `pending/running` run 继续按当前决策标记为 `failed`；本方案不增加 run 恢复、lease
续租或 revision。

桌面运行边界保持当前实现事实：Tauri Rust host 通过
`apps/desktop/src-tauri/src/backend_supervisor.rs` / `backend_process.rs` 启动和停止本机
Python/FastAPI 进程，等待 backend readiness，并通过 runtime config 将实际 loopback URL
交给 React。FastAPI 数据库位于 backend storage，snapshot 也存放在同一个 SQLite 主库。

后端崩溃时：

1. Tauri supervisor 通过子进程退出和 readiness 状态感知 backend 失败；
2. 本次 Python 进程内的 executor task、内存 snapshot 和 controller 连接全部失效；
3. 新 backend 进程启动时执行既有启动 recovery，把遗留 `pending/running` run 标记为
   `failed`；
4. 前端检测连接失败并显示 backend 状态/重试入口；
5. 用户重新打开 Task 时从 `conversation_task_snapshots` hydrate 完整 state；
6. 用户发送“继续”时创建新的 command/run，不尝试恢复旧的 asyncio task。

本方案不增加跨进程 Transport resume，也不把内存 notifier 当作崩溃恢复机制。

### 6.4 持久化节奏

第一版采用“每个已接受的 Runtime mutation 与 snapshot 在同一个 SQLite 写事务中提交”的
简单、可验证策略，不做 Transport 先发、snapshot 延迟落盘的批量窗口。顺序固定为：

```text
Runtime mutation
  → 更新内存 snapshot
  → 在事务内写入 state_json
  → commit 成功
  → 发送 set / append-text
```

这样不会出现 Transport 已显示而持久化 snapshot 丢失的窗口。SQLite 写入失败时：

- 事务回滚；
- 不发送本次 mutation；
- Runtime 收到异常并停止依赖该 mutation 的后续执行；
- 下次连接从上一个已提交 snapshot hydrate。

每个文本 chunk 重写 JSON 的写放大问题先以可验证性为优先；只有在 profiling 证明需要时，
才另行设计“事务内合并多个 mutation”的 snapshot store。未来批量化仍必须保持同一原则：
只有 durable snapshot commit 成功后才能对 Transport 发布合并 mutation，并用内存队列丢弃
未提交 mutation，不新增 revision 或 outbox。

### 6.5 后续 notifier

第一版可以先保持当前 `ConversationRunSubscriptionService` 的轮询，只把轮询输入从“重新
重建数据库 snapshot”改为“等待 snapshot owner 发布的 mutation”。后续用进程内
`ConversationRunChangeNotifier` 替代 `asyncio.sleep(0.05)`：

```text
snapshot commit 成功
  → notifier.publish(task_id, mutations)
  → 每个 stream callback 顺序消费 mutation
  → 修改自己的 controller.state
```

notifier 只负责唤醒当前进程内订阅者，不负责持久化和补偿。订阅建立时先 hydrate 当前
持久化 snapshot，再注册 notifier；注册窗口内的 mutation 必须通过“注册后重新读取/检查
当前 snapshot”补齐。断线或进程重启时直接从持久化 snapshot 重新 hydrate，因此不依赖
notifier 不丢消息。

## 7. 代码改造边界

### 7.1 后端 Assistant Transport

重点改造：

```text
apps/backend/app/assistant_transport/assistant_api.py
```

它只负责编排：创建/加载 snapshot、创建 `assistant-stream`、初始化 controller、连接
stream coordinator；不再在端点内做 snapshot diff 或根 state 替换。

建议新增：

```text
apps/backend/app/assistant_transport/stream/
```

至少拆出：

- snapshot mutation 类型和路径操作；
- controller state mutation adapter；
- stream 生命周期、订阅和终态协调。

Assistant Transport 是唯一可以依赖 `assistant_stream` 的边界。`ConversationStateSnapshot`
等中性 JSON 契约不能继续放在 `app.assistant_transport.state`，否则 service 会反向依赖
Transport；这些类型应随下节的 domain/model 边界迁移。

### 7.2 Runtime/Agent

以下文件保留职责，不让 Agent 依赖 Transport：

```text
apps/backend/app/core/runtime/runtime_operations.py
apps/backend/app/core/workflows/nodes/model_node.py
apps/backend/app/core/workflows/nodes/tools_node.py
```

当前代码事实是 `RuntimeOperations`、`runner.py`、`tools_node.py` 通过
`app.assistant_transport.service.conversation_mutation_writer` 取得 writer；这已经把
Assistant Transport 目录反向带入 core/service，违反本项目的依赖边界。目标改造必须将
canonical mutation writer 和其 port 移到低耦合的 service 边界，例如：

```text
apps/backend/app/service/task/conversation_mutation_writer.py
apps/backend/app/service/task/conversation_mutation_port.py
```

具体文件名可按一文件一主类最终确定，但不得保留长期兼容 re-export。目标依赖为：

```text
core → service mutation port
assistant_transport API → service mutation/snapshot port
service → storage
```

`assistant_stream` 和 `controller.state` 只能出现在 `assistant_transport` API/stream
适配边界。Runtime/Agent 继续产生意图明确的 mutation：文本、reasoning、tool-call、tool
状态、tool result、run 终态；不 import `assistant_stream`，不直接操作 `controller.state`。

同时迁移当前位于 `app.assistant_transport.state` 的中性契约：

```text
apps/backend/app/models/conversation_state_snapshot.py
apps/backend/app/models/conversation_state_part.py
```

或按现有 models 包的一文件一主类型约定拆分为同一 `app.models` 子包。目标是让
`ConversationStateService`、`ConversationRunSubscriptionService`、core 和 API 都依赖
`app.models` 的中性类型；`assistant_transport.state` 目录在调用者迁移完成后删除，不保留
兼容 re-export。这里的中性类型只描述 JSON 形状，不 import assistant-ui、`assistant_stream`
或 service。

### 7.3 Storage

新增：

```text
apps/backend/app/storage/model/conversation_task_snapshot_model.py
apps/backend/app/storage/crud/conversation_task_snapshot_crud.py
```

并在：

```text
apps/backend/app/storage/init_schema.py
```

注册模型和 schema 演进规则。Task 删除时必须删除对应 snapshot，不得留下孤儿行。

现有 `conversation_messages`、`conversation_message_parts`、`conversation_tool_calls` 是否
最终删除或保留，属于独立的事实模型迁移决策；本方案不把 context 重建混入其中，但过渡期
必须明确 Assistant Transport 只读 snapshot，不能出现实时读 snapshot、历史读旧表的长期双读。

### 7.4 前端

```text
apps/desktop/components/assistant/assistant-runtime.tsx
apps/desktop/lib/assistant/contract.ts
apps/desktop/lib/assistant/converter.ts
```

继续使用 `useAssistantTransportRuntime`；converter 继续负责中性 snapshot 到 assistant-ui
消息的映射。前端不执行 tool、不持久化业务事实、不回传 state 作为后端事实。

## 8. 实施顺序

1. 将中性 `ConversationStateSnapshot` / part 契约从 `assistant_transport.state` 迁移到
   `app.models`，先清理 service/core 的反向依赖，不保留兼容 re-export。
2. 固化 snapshot 字段和 `schema_version`。
3. 新增一对一 snapshot model、CRUD、schema 注册和 Task 删除级联。
4. 实现唯一 snapshot owner 及其 mutation port，并把 `ConversationMutationWriter` 移到
   service 边界。
5. 将 Task/run 初始消息和初始 snapshot 放入一致事务。
6. 先实现文本 `append-text`，移除正常路径的完整 state 替换。
7. 实现新增 message/part 和状态字段的最小 `set`。
8. 实现 tool-call 创建、状态、结果、错误、取消和审批摘要的增量传输。
9. 将订阅服务改为消费 mutation，不再查询数据库重建和全量 diff。
10. 验证断线、重连、取消、失败、终态和进程重启场景。
11. 后续单独用进程内 notifier 替代 `50ms` 轮询。
12. 新旧双读/双写只允许存在于可验证的过渡窗口，验收后清理，不作为长期结构。

## 9. 验收标准

### Snapshot

- 每个 Task 最多一行 snapshot；
- 重复初始化不会产生重复行；
- JSON 可完整反序列化，schema version 不支持时显式失败；
- Task 删除不会留下孤儿 snapshot；
- run 创建和初始 snapshot 不出现半提交状态。

### Mutation

- 文本追加产生 `append-text`，value 只有新增文本；
- 不重复追加、不替换整个 messages 数组；
- 新消息和新 part 使用对应位置的 `set`；
- tool pending/running/requires-action/completed/failed/cancelled 均有最小路径更新；
- tool result/error/isError 正确更新；
- 无变化不产生重复 mutation；
- snapshot 持久化失败时不得向 Transport 宣布成功。

### 协议和前端

- 首次连接发送完整 state；
- 后续文本帧包含 `append-text`；
- tool 状态和结果帧只更新最小路径；
- 真实 `assistant-stream` 输出不重复传输完整 messages；
- converter 正确映射 text/reasoning/tool/status/error/cancelled；
- 前端不回传业务 state。

### 架构

- core、tools、models、storage 不 import `assistant_stream` 或 assistant-ui；
- `app.service.*`、`app.core.*` 不 import `app.assistant_transport.*`；
- `app.models` 的 snapshot/part 契约不依赖 service、API 或 Transport；
- 不新增 RuntimeEvent、revision、outbox 或分布式队列；
- 不引入认证、多租户、Redis、Postgres 或公网服务依赖；
- context 构建保持现状，不混入本次验收；
- 关键实现必须经过独立子 Agent 验收。

## 10. 结论

本次不是在现有全量 snapshot 逻辑上增加 diff 补丁，而是改变状态更新所有权：

```text
Runtime mutation
  → 唯一 ConversationTaskSnapshot
  → 明确的 set / append-text mutation
  → Assistant Transport
```

数据库保存一个 Task 的完整 JSON snapshot；进程内快照负责实时运行；`controller.state` 只
负责把增量操作传给前端。文本和 tool-call 生命周期共享同一机制，Agent 层不承担 Transport
协议细节，context 重建另行设计。
